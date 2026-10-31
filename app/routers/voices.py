"""Voice bank API: cloned-voice CRUD, sample handling and preview."""

import asyncio
import uuid

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response

from app.config import (
    ALLOWED_SAMPLE_TYPES,
    MAX_SAMPLE_BYTES,
    PREVIEW_TEXT,
    SAMPLE_WARN_BYTES,
    ensure_dirs,
)
from app.models import PreviewRequest, VoicePatch
from app.services import voiceover as voiceover_service
from app.storage import (
    delete_voice,
    get_voice,
    load_style_presets,
    load_voices,
    now_iso,
    sample_path,
    save_voices,
)
from app.config import SAMPLES_DIR

router = APIRouter(prefix="/api/voices", tags=["voices"])
presets_router = APIRouter(prefix="/api/style-presets", tags=["voices"])


def _resolve_type(file: UploadFile) -> tuple[str, str]:
    match = ALLOWED_SAMPLE_TYPES.get((file.content_type or "").lower())
    if match is None:
        name = (file.filename or "").lower()
        if name.endswith(".mp3"):
            match = ("mp3", "audio/mpeg")
        elif name.endswith(".wav"):
            match = ("wav", "audio/wav")
    if match is None:
        raise HTTPException(status_code=400, detail="only mp3 or wav samples are supported")
    return match


async def _read_sample(file: UploadFile) -> bytes:
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="empty sample file")
    if len(raw) > MAX_SAMPLE_BYTES:
        raise HTTPException(status_code=413, detail="sample too large (max 7 MB)")
    return raw


def _public(voice_id: str, voice: dict) -> dict:
    return {
        "id": voice_id,
        "name": voice.get("name", ""),
        "filename": voice.get("filename", ""),
        "mime": voice.get("mime", ""),
        "size_bytes": voice.get("size_bytes", 0),
        # The sample is re-uploaded on every synthesis request, so a big sample
        # is a real cost on a 75-scene project.
        "sample_warning": voice.get("size_bytes", 0) > SAMPLE_WARN_BYTES,
        "speed": voice.get("speed", 0.97),
        "stability": voice.get("stability", 45),
        "similarity": voice.get("similarity", 75),
        "style": voice.get("style", 9),
        "speaker_boost": voice.get("speaker_boost", True),
        "category": voice.get("category", ""),
        "description": voice.get("description", ""),
        "preset_id": voice.get("preset_id", ""),
        "created_at": voice.get("created_at", ""),
        "style_prompt": voiceover_service.build_style_prompt(voice),
    }


@router.get("")
def list_all():
    voices = load_voices()
    return {"voices": [_public(vid, v) for vid, v in voices.items()]}


@router.get("/{voice_id}")
def get_one(voice_id: str):
    return _public(voice_id, get_voice(voice_id))


@router.post("", status_code=201)
async def create_voice(
    file: UploadFile = File(...),
    name: str = Form(...),
    speed: float = Form(0.97),
    stability: float = Form(45.0),
    similarity: float = Form(75.0),
    style: float = Form(9.0),
    speaker_boost: bool = Form(True),
    category: str = Form(""),
    description: str = Form(""),
    preset_id: str = Form(""),
):
    ensure_dirs()
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    ext, mime = _resolve_type(file)
    raw = await _read_sample(file)

    voice_id = uuid.uuid4().hex[:8]
    filename = f"{voice_id}.{ext}"
    (SAMPLES_DIR / filename).write_bytes(raw)

    voices = load_voices()
    voices[voice_id] = {
        "name": name,
        "filename": filename,
        "mime": mime,
        "size_bytes": len(raw),
        "speed": speed,
        "stability": stability,
        "similarity": similarity,
        "style": style,
        "speaker_boost": speaker_boost,
        "category": category.strip(),
        "description": description.strip(),
        "preset_id": preset_id.strip(),
        "created_at": now_iso(),
    }
    save_voices(voices)
    return _public(voice_id, voices[voice_id])


@router.patch("/{voice_id}")
def update_voice(voice_id: str, payload: VoicePatch):
    voices = load_voices()
    voice = voices.get(voice_id)
    if not voice:
        raise HTTPException(status_code=404, detail="voice not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        if value is None:
            continue
        voice[field] = value.strip() if isinstance(value, str) else value
    save_voices(voices)
    return _public(voice_id, voice)


@router.post("/{voice_id}/sample")
async def replace_sample(voice_id: str, file: UploadFile = File(...)):
    voices = load_voices()
    voice = voices.get(voice_id)
    if not voice:
        raise HTTPException(status_code=404, detail="voice not found")
    ext, mime = _resolve_type(file)
    raw = await _read_sample(file)

    old = sample_path(voice)
    filename = f"{voice_id}.{ext}"
    (SAMPLES_DIR / filename).write_bytes(raw)
    if old.exists() and old.name != filename:
        old.unlink()

    voice.update({"filename": filename, "mime": mime, "size_bytes": len(raw)})
    save_voices(voices)
    return _public(voice_id, voice)


@router.get("/{voice_id}/sample")
def get_sample(voice_id: str):
    voice = get_voice(voice_id)
    path = sample_path(voice)
    if not path.exists():
        raise HTTPException(status_code=410, detail="voice sample missing on disk")
    return FileResponse(path, media_type=voice.get("mime", "audio/mpeg"))


@router.delete("/{voice_id}", status_code=204)
def remove_voice(voice_id: str):
    delete_voice(voice_id)
    return Response(status_code=204)


@router.post("/{voice_id}/preview")
async def preview(voice_id: str, payload: PreviewRequest):
    voice = get_voice(voice_id)
    text = (payload.text or "").strip() or PREVIEW_TEXT
    sample_b64 = voiceover_service.read_sample_b64(voice)
    style_prompt = voiceover_service.build_style_prompt(voice)
    wav = await asyncio.to_thread(
        voiceover_service.synthesize, text[:800], sample_b64, voice["mime"], style_prompt
    )
    if not wav:
        raise HTTPException(status_code=502, detail="preview returned no audio")
    return Response(content=wav, media_type="audio/wav")


@presets_router.get("")
def list_presets():
    presets = load_style_presets()
    return {
        "presets": [
            {"id": pid, "name": cfg.get("name", pid), "prompt": cfg.get("prompt", "")}
            for pid, cfg in presets.items()
        ]
    }
