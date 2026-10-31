"""Project API: the five wizard steps plus exports."""

import asyncio
import io
import zipfile
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse, Response

from app.config import (
    DEFAULT_CATEGORY,
    DEFAULT_LANGUAGE,
    DEFAULT_WPM,
    LANGUAGES,
    SCENE_SECONDS,
)
from app.export import write_exports
from app.models import ProjectCreate, ProjectPatch, ScriptRequest, VoiceoverRequest
from app.services import scenes as scenes_service
from app.services import script as script_service
from app.services import titles as titles_service
from app.services import voiceover as voiceover_service
from app.storage import (
    audio_dir,
    delete_project,
    list_projects,
    load_niche,
    load_project,
    new_project,
    now_iso,
    project_dir,
    update_project,
)

router = APIRouter(prefix="/api/projects", tags=["projects"])


def _niche_snapshot(niche_id: str) -> dict:
    """Copy a niche's profile into a project's context.

    Snapshotted rather than looked up on every call: re-analysing a niche must
    not silently rewrite the reference a finished project was written against.
    A missing niche is a 404 here rather than a silently empty profile, so a
    typo in the id cannot quietly produce a project with no reference at all.
    """
    niche_id = (niche_id or "").strip()
    if not niche_id:
        return {"niche_id": "", "niche_name": "", "niche_profile": {}}
    doc = load_niche(niche_id)
    return {
        "niche_id": doc["id"],
        "niche_name": doc.get("name", ""),
        "niche_profile": doc.get("profile") or {},
    }


def _context(payload: ProjectCreate) -> dict:
    language = payload.language if payload.language in LANGUAGES else DEFAULT_LANGUAGE
    custom = payload.max_words is not None
    target, max_words = script_service.resolve_budget(
        {
            "minutes": payload.minutes,
            "wpm": payload.wpm,
            "max_words": payload.max_words,
            "max_words_custom": custom,
        }
    )
    return {
        "topic": payload.topic.strip(),
        "category": (payload.category or DEFAULT_CATEGORY).strip(),
        "language": language,
        "reference": (payload.reference or "").strip(),
        "minutes": payload.minutes,
        "wpm": payload.wpm or DEFAULT_WPM,
        "scene_seconds": payload.scene_seconds or SCENE_SECONDS,
        "max_words": max_words,
        # Without this, the derived ceiling would be read back as a deliberate
        # override and would never track a later change to length or pace.
        "max_words_custom": custom,
        "target_words": target,
        **_niche_snapshot(payload.niche_id),
    }


@router.get("")
def get_projects():
    return {"projects": list_projects()}


@router.post("", status_code=201)
def create_project(payload: ProjectCreate):
    return new_project(_context(payload))


@router.get("/{project_id}")
def get_project(project_id: str):
    return load_project(project_id)


@router.patch("/{project_id}")
def patch_project(project_id: str, payload: ProjectPatch):
    # Resolved before the write lock: it reads another store and can 404, and
    # neither belongs inside a held lock.
    snapshot = _niche_snapshot(payload.niche_id) if payload.niche_id is not None else {}

    def mutate(doc: dict) -> None:
        ctx = doc["context"]

        for field in ("topic", "category", "reference"):
            value = getattr(payload, field)
            if value is not None:
                ctx[field] = value.strip()
        if payload.language is not None and payload.language in LANGUAGES:
            ctx["language"] = payload.language
        if payload.niche_id is not None:
            ctx.update(snapshot)
        for field in ("minutes", "wpm", "max_words", "scene_seconds"):
            value = getattr(payload, field)
            if value is not None:
                ctx[field] = value
        if payload.max_words is not None:
            ctx["max_words_custom"] = True
        ctx["target_words"], ctx["max_words"] = script_service.resolve_budget(ctx)

        if payload.selected_title is not None:
            doc["selected_title"] = payload.selected_title.strip()
            if doc["selected_title"]:
                doc["step"] = max(doc.get("step", 1), 3)

        if payload.script is not None and payload.script.strip() != doc.get("script", ""):
            doc["script"] = payload.script.strip()
            words = script_service.word_count(doc["script"])
            doc["script_meta"] = {
                **doc.get("script_meta", {}),
                "word_count": words,
                "estimated_minutes": round(words / (ctx.get("wpm") or DEFAULT_WPM), 2),
                "edited": True,
                "over_budget": words > ctx["max_words"],
            }
            # An edit is not a regeneration: the existing scenes are kept,
            # because discarding dozens of hand-checked prompts over a typo fix
            # would be worse than the mismatch. They are flagged as stale.
            if doc.get("scenes"):
                doc["scenes_stale"] = True
            if doc["script"]:
                doc["step"] = max(doc.get("step", 1), 4)

    doc = update_project(project_id, mutate)
    write_exports(doc)
    return doc


@router.delete("/{project_id}", status_code=204)
def remove_project(project_id: str):
    delete_project(project_id)
    return Response(status_code=204)


# --- Step 2: titles ---------------------------------------------------------


@router.post("/{project_id}/titles")
async def generate_titles(project_id: str):
    doc = load_project(project_id)
    results = await asyncio.to_thread(titles_service.generate_titles, doc["context"])
    if not results:
        raise HTTPException(status_code=502, detail="MIMO returned no usable titles")

    def mutate(current: dict) -> None:
        current["titles"] = results
        current["step"] = max(current.get("step", 1), 2)

    # Re-read under the lock: the call above took a while and the document may
    # have moved on.
    update_project(project_id, mutate)
    return {"titles": results}


# --- Step 3: script ---------------------------------------------------------


@router.post("/{project_id}/script")
async def generate_script(project_id: str, payload: ScriptRequest):
    doc = load_project(project_id)
    ctx = dict(doc["context"])
    for field in ("minutes", "wpm", "max_words"):
        value = getattr(payload, field)
        if value is not None:
            ctx[field] = value
    ctx["target_words"], ctx["max_words"] = script_service.resolve_budget(ctx)

    title = doc.get("selected_title") or ctx.get("topic", "")
    if not title:
        raise HTTPException(status_code=400, detail="pick a title first")

    result = await asyncio.to_thread(
        script_service.generate_script, ctx, title, payload.notes
    )
    if not result["script"]:
        raise HTTPException(status_code=502, detail="MIMO returned an empty script")

    meta = {k: v for k, v in result.items() if k != "script"}

    def mutate(current: dict) -> None:
        current["context"] = ctx
        current["script"] = result["script"]
        current["script_meta"] = meta
        current["step"] = max(current.get("step", 1), 4)
        # The script changed, so any existing scene breakdown is stale.
        current["scenes"] = []
        current["thumbnail_prompt"] = ""
        current["scenes_stale"] = False
        current["voiceover"] = {"status": "idle"}

    doc = update_project(project_id, mutate)
    write_exports(doc)
    return {"script": doc["script"], **meta}


# --- Step 4: scenes ---------------------------------------------------------


@router.post("/{project_id}/scenes")
async def generate_scenes(project_id: str):
    doc = load_project(project_id)
    if not doc.get("script"):
        raise HTTPException(status_code=400, detail="generate a script first")

    result = await scenes_service.build_scenes(doc)
    if not result["scenes"]:
        raise HTTPException(status_code=502, detail="scene breakdown produced nothing")

    dropped = result.pop("dropped_scenes", 0)

    def mutate(current: dict) -> None:
        current.update(result)
        current["scenes_stale"] = False
        current["step"] = max(current.get("step", 1), 5)

    doc = update_project(project_id, mutate)
    write_exports(doc)
    failed = [s["index"] for s in doc["scenes"] if s["status"] != "ok"]
    return {
        "scenes": doc["scenes"],
        "thumbnail_prompt": doc["thumbnail_prompt"],
        "style_bible": doc["style_bible"],
        "negative_prompt": doc.get("negative_prompt", ""),
        "failed": failed,
        # Non-zero means the script was longer than the scene cap allows and
        # the tail is not covered by any prompt.
        "dropped_scenes": dropped,
    }


@router.post("/{project_id}/scenes/retry")
async def retry_scenes(project_id: str):
    doc = load_project(project_id)
    if not doc.get("scenes"):
        raise HTTPException(status_code=400, detail="no scenes to retry")
    repaired = await scenes_service.retry_failed_scenes(doc)

    def mutate(current: dict) -> None:
        current["scenes"] = repaired

    doc = update_project(project_id, mutate)
    write_exports(doc)
    failed = [s["index"] for s in doc["scenes"] if s["status"] != "ok"]
    return {"scenes": doc["scenes"], "failed": failed}


# --- Step 5: voice-over -----------------------------------------------------


@router.post("/{project_id}/voiceover", status_code=202)
def start_voiceover(project_id: str, payload: VoiceoverRequest, background: BackgroundTasks):
    doc = load_project(project_id)
    if not doc.get("script"):
        raise HTTPException(status_code=400, detail="write a script first")
    if voiceover_service.is_running(project_id):
        raise HTTPException(status_code=409, detail="a voice-over job is already running")

    background.add_task(voiceover_service.run_job, project_id, payload.voice_id)
    return {"status": "queued"}


@router.get("/{project_id}/voiceover/status")
def voiceover_status(project_id: str):
    doc = load_project(project_id)
    state = dict(doc.get("voiceover", {"status": "idle"}))
    running = voiceover_service.is_running(project_id)

    # "running" is persisted but the job itself only lives in this process. If
    # the server restarted mid-job the document would claim it is still going
    # and the browser would poll forever, so the state is corrected on read.
    if state.get("status") == "running" and not running:
        state["status"] = "interrupted"
        state["error"] = "The server restarted while this job was running."
        state["finished_at"] = state.get("finished_at") or now_iso()

        def mutate(current: dict) -> None:
            current["voiceover"] = state

        doc = update_project(project_id, mutate)

    state["running"] = running
    return state


@router.post("/{project_id}/voiceover/retry", status_code=202)
def retry_voiceover(project_id: str, background: BackgroundTasks):
    doc = load_project(project_id)
    state = doc.get("voiceover", {})
    if not state.get("voice_id"):
        raise HTTPException(status_code=400, detail="no previous voice to retry with")
    if state.get("status") == "done":
        raise HTTPException(status_code=400, detail="the track is already complete")
    if voiceover_service.is_running(project_id):
        raise HTTPException(status_code=409, detail="a voice-over job is already running")
    # Only the pieces missing from disk are re-synthesized.
    background.add_task(
        voiceover_service.run_job, project_id, state["voice_id"], True
    )
    return {"status": "queued"}


# --- Files ------------------------------------------------------------------


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


@router.get("/{project_id}/audio/{filename}")
def get_audio(project_id: str, filename: str):
    return FileResponse(_safe_file(audio_dir(project_id), filename), media_type="audio/wav")


@router.get("/{project_id}/export/script.txt")
def export_script(project_id: str):
    doc = load_project(project_id)
    if not doc.get("script"):
        raise HTTPException(status_code=404, detail="no script yet")
    write_exports(doc)
    name = f"{project_id}-script.txt"
    return FileResponse(
        project_dir(project_id) / "script.txt", media_type="text/plain", filename=name
    )


@router.get("/{project_id}/export/prompts.csv")
def export_prompts(project_id: str):
    doc = load_project(project_id)
    if not doc.get("scenes"):
        raise HTTPException(status_code=404, detail="no scenes yet")
    write_exports(doc)
    name = f"{project_id}-prompts.csv"
    return FileResponse(
        project_dir(project_id) / "prompts.csv", media_type="text/csv", filename=name
    )


@router.get("/{project_id}/export/bundle.zip")
def export_bundle(project_id: str):
    doc = load_project(project_id)
    write_exports(doc)
    base = project_dir(project_id)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.name.endswith(".tmp"):
                continue
            relative = path.relative_to(base).as_posix()
            # The per-request pieces exist only so a retry can skip what it
            # already rendered. The deliverable is the full track.
            if relative.startswith("audio/chunks/"):
                continue
            zf.write(path, relative)
    buf.seek(0)
    return Response(
        content=buf.read(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{project_id}.zip"'},
    )
