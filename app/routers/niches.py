"""Niche bank API: reference material in, one reusable profile out."""

import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response

from app.config import (
    ALLOWED_IMAGE_TYPES,
    ALLOWED_VIDEO_TYPES,
    MAX_SHOT_BYTES,
    MAX_SHOTS,
    MAX_VIDEO_BYTES,
    ensure_dirs,
)
from app.models import NicheCreate, NichePatch
from app.services import niche as niche_service
from app.storage import (
    delete_niche,
    list_niches,
    load_niche,
    new_niche,
    niche_video_dir,
    now_iso,
    shots_dir,
    update_niche,
)

router = APIRouter(prefix="/api/niches", tags=["niches"])


def _safe_file(base: Path, filename: str) -> Path:
    """Resolve a filename inside `base`, rejecting anything that escapes it.

    `Path("..").name` is `".."`, so stripping to the basename is not enough on
    its own; the resolved path is checked to be a direct child of `base`.
    """
    root = base.resolve()
    path = (root / Path(filename).name).resolve()
    if path.parent != root or not path.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return path


def _resolve_image(file: UploadFile) -> tuple[str, str]:
    match = ALLOWED_IMAGE_TYPES.get((file.content_type or "").lower())
    if match is None:
        suffix = Path(file.filename or "").suffix.lower().lstrip(".")
        for ext, mime in ALLOWED_IMAGE_TYPES.values():
            if suffix in (ext, "jpeg" if ext == "jpg" else ext):
                match = (ext, mime)
                break
    if match is None:
        raise HTTPException(
            status_code=400,
            detail="screenshots must be bmp, gif, png, jpeg or webp",
        )
    return match


@router.get("")
def get_niches():
    return {"niches": list_niches()}


@router.post("", status_code=201)
def create_niche(payload: NicheCreate):
    return new_niche(payload.name)


@router.get("/{niche_id}")
def get_niche(niche_id: str):
    return load_niche(niche_id)


@router.patch("/{niche_id}")
def patch_niche(niche_id: str, payload: NichePatch):
    def mutate(doc: dict) -> None:
        for field in ("name", "notes", "transcript"):
            value = getattr(payload, field)
            if value is not None:
                doc[field] = value.strip()
        if payload.youtube_url is not None:
            source = dict(doc.get("source", {}))
            url = payload.youtube_url.strip()
            if url != source.get("youtube_url"):
                # A different link means the downloaded video no longer
                # belongs to this niche. Forget it rather than analyse the
                # old one against the new URL.
                source.update({"video_file": "", "downloaded_at": "", "duration_sec": 0})
            source["youtube_url"] = url
            doc["source"] = source

    return update_niche(niche_id, mutate)


@router.delete("/{niche_id}", status_code=204)
def remove_niche(niche_id: str):
    delete_niche(niche_id)
    return Response(status_code=204)


# --- Screenshots ------------------------------------------------------------


@router.post("/{niche_id}/shots", status_code=201)
async def add_shots(niche_id: str, files: list[UploadFile] = File(...)):
    doc = load_niche(niche_id)
    directory = shots_dir(niche_id)
    directory.mkdir(parents=True, exist_ok=True)

    if len(doc.get("shots", [])) + len(files) > MAX_SHOTS:
        raise HTTPException(
            status_code=400, detail=f"a niche holds at most {MAX_SHOTS} screenshots"
        )

    added = []
    for file in files:
        ext, mime = _resolve_image(file)
        raw = await file.read()
        if not raw:
            raise HTTPException(status_code=400, detail="empty screenshot")
        if len(raw) > MAX_SHOT_BYTES:
            raise HTTPException(status_code=413, detail="screenshot too large (max 8 MB)")
        filename = f"{uuid.uuid4().hex[:8]}.{ext}"
        (directory / filename).write_bytes(raw)
        added.append(
            {
                "filename": filename,
                "mime": mime,
                "size_bytes": len(raw),
                "note": "",
                "added_at": now_iso(),
            }
        )

    def mutate(current: dict) -> None:
        current["shots"] = list(current.get("shots", [])) + added

    return update_niche(niche_id, mutate)


@router.get("/{niche_id}/shots/{filename}")
def get_shot(niche_id: str, filename: str):
    doc = load_niche(niche_id)
    path = _safe_file(shots_dir(niche_id), filename)
    mime = next(
        (s.get("mime") for s in doc.get("shots", []) if s.get("filename") == path.name),
        "image/png",
    )
    return FileResponse(path, media_type=mime)


@router.delete("/{niche_id}/shots/{filename}")
def remove_shot(niche_id: str, filename: str):
    path = _safe_file(shots_dir(niche_id), filename)
    path.unlink()

    def mutate(current: dict) -> None:
        current["shots"] = [
            s for s in current.get("shots", []) if s.get("filename") != path.name
        ]

    return update_niche(niche_id, mutate)


# --- Video ------------------------------------------------------------------


@router.post("/{niche_id}/video")
async def upload_video(niche_id: str, file: UploadFile = File(...)):
    load_niche(niche_id)
    match = ALLOWED_VIDEO_TYPES.get((file.content_type or "").lower())
    if match is None and not (file.filename or "").lower().endswith((".mp4", ".mov")):
        raise HTTPException(status_code=400, detail="the reference video must be an mp4")

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="empty video file")
    if len(raw) > MAX_VIDEO_BYTES:
        raise HTTPException(status_code=413, detail="video too large (max 200 MB)")

    directory = niche_video_dir(niche_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "source.mp4").write_bytes(raw)

    def mutate(current: dict) -> None:
        source = dict(current.get("source", {}))
        source.update(
            {"video_file": "source.mp4", "downloaded_at": now_iso(), "duration_sec": 0}
        )
        current["source"] = source

    return update_niche(niche_id, mutate)


@router.get("/{niche_id}/video")
def get_video(niche_id: str):
    doc = load_niche(niche_id)
    name = doc.get("source", {}).get("video_file")
    if not name:
        raise HTTPException(status_code=404, detail="this niche has no video")
    return FileResponse(_safe_file(niche_video_dir(niche_id), name), media_type="video/mp4")


@router.delete("/{niche_id}/video")
def remove_video(niche_id: str):
    doc = load_niche(niche_id)
    name = doc.get("source", {}).get("video_file")
    if name:
        path = niche_video_dir(niche_id) / Path(name).name
        if path.is_file():
            path.unlink()

    def mutate(current: dict) -> None:
        source = dict(current.get("source", {}))
        source.update({"video_file": "", "downloaded_at": "", "duration_sec": 0})
        current["source"] = source

    return update_niche(niche_id, mutate)


# --- Analysis ---------------------------------------------------------------


@router.post("/{niche_id}/analyse", status_code=202)
def start_analysis(niche_id: str, background: BackgroundTasks):
    doc = load_niche(niche_id)
    if niche_service.is_running(niche_id):
        raise HTTPException(status_code=409, detail="an analysis is already running")

    source = doc.get("source", {})
    has_material = bool(
        source.get("youtube_url")
        or source.get("video_file")
        or doc.get("shots")
        or (doc.get("transcript") or "").strip()
    )
    if not has_material:
        raise HTTPException(
            status_code=400,
            detail=(
                "There is nothing to analyse. Add a YouTube link, a video, "
                "screenshots, or paste a transcript first."
            ),
        )

    ensure_dirs()
    background.add_task(niche_service.run_job, niche_id)
    return {"status": "queued"}


@router.get("/{niche_id}/status")
def analysis_status(niche_id: str):
    doc = load_niche(niche_id)
    state = dict(doc.get("analysis", {"status": "idle"}))
    running = niche_service.is_running(niche_id)

    # "running" is persisted but the job only ever lived in this process. After
    # a restart the document would claim it is still going and the browser
    # would poll forever, so the state is corrected on read.
    if state.get("status") == "running" and not running:
        state["status"] = "interrupted"
        state["error"] = "The server restarted while this analysis was running."
        state["finished_at"] = state.get("finished_at") or now_iso()
        update_niche(niche_id, lambda current: current.update({"analysis": state}))

    state["running"] = running
    return state
