"""File-backed stores for projects, voices and style presets.

There is no database. A project is a directory under `data/projects/` whose
`project.json` is the single source of truth; the exported `.txt`, `.csv` and
`.wav` files are derived artefacts that live beside it.
"""

import json
import os
import re
import shutil
import threading
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException

from app.config import (
    NICHES_DIR,
    PROJECTS_DIR,
    SAMPLES_DIR,
    STYLE_PRESETS_PATH,
    VOICES_PATH,
    ensure_dirs,
)

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
# Ids are produced by slugify() plus a uuid suffix, so this is the full shape.
_SAFE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,80}")


def now_iso() -> str:
    """Timestamp for the stores, to the millisecond.

    Seconds are not enough resolution: the listings sort newest-first on this
    value, and two things created inside the same second would come back in
    whatever order the filesystem happened to hand them over. Records written
    at the old precision still sort correctly against new ones, because '+'
    sorts before '.' and a second-precision stamp is the older of the two.
    """
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def slugify(text: str, max_len: int = 40) -> str:
    slug = _SLUG_STRIP.sub("-", text.lower()).strip("-")
    return slug[:max_len].strip("-") or "project"


def _write_json(path: Path, payload) -> None:
    """Write JSON atomically so a crash mid-write cannot corrupt the store.

    The temporary name is unique per call: a shared one would let two
    concurrent writers of the same file interleave into each other's buffer,
    which is exactly the corruption the rename is supposed to prevent.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


# --- Projects ---------------------------------------------------------------


def project_dir(project_id: str) -> Path:
    """Resolve a project id to its directory, rejecting anything else.

    The id is matched against a positive pattern rather than filtered. A
    round-trip check through `Path(...).name` looks equivalent but is not:
    `Path("..").name` is `".."`, so `".."` would pass and the directory would
    resolve to the data root — and `DELETE /api/projects/%2e%2e` would delete
    every project and the entire voice bank.
    """
    if not _SAFE_ID.fullmatch(project_id or ""):
        raise HTTPException(status_code=400, detail="invalid project id")
    path = PROJECTS_DIR / project_id
    # Belt and braces: whatever the pattern allowed, it must stay a direct child.
    if path.resolve().parent != PROJECTS_DIR.resolve():
        raise HTTPException(status_code=400, detail="invalid project id")
    return path


def audio_dir(project_id: str) -> Path:
    return project_dir(project_id) / "audio"


def project_path(project_id: str) -> Path:
    return project_dir(project_id) / "project.json"


def new_project(context: dict) -> dict:
    ensure_dirs()
    pid = f"{slugify(context['topic'])}-{uuid.uuid4().hex[:6]}"
    doc = {
        "id": pid,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "context": context,
        "titles": [],
        "selected_title": "",
        "script": "",
        "script_meta": {},
        "style_bible": "",
        "scenes": [],
        "thumbnail_prompt": "",
        "voiceover": {"status": "idle"},
        "step": 1,
    }
    audio_dir(pid).mkdir(parents=True, exist_ok=True)
    save_project(doc)
    return doc


def save_project(doc: dict) -> dict:
    doc["updated_at"] = now_iso()
    _write_json(project_path(doc["id"]), doc)
    return doc


def load_project(project_id: str) -> dict:
    doc = _read_json(project_path(project_id), None)
    if doc is None:
        raise HTTPException(status_code=404, detail="project not found")
    return doc


_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


@contextmanager
def _entity_lock(key: str):
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(key, threading.Lock())
    with lock:
        yield


@contextmanager
def project_lock(project_id: str):
    """Serialize read-modify-write cycles on one project.

    Requests run in a thread pool while the voice-over job runs on the event
    loop, so a plain load/mutate/save pair can be interleaved by another writer
    and lose whichever update finished first. This is the only thing standing
    between the two.

    Hold it for the write cycle only — never across a MIMO call.

    The key is namespaced because niches share this lock table, and a niche
    and a project are free to carry the same id.
    """
    with _entity_lock(f"project:{project_id}"):
        yield


def update_project(project_id: str, mutate: Callable[[dict], None]) -> dict:
    """Apply `mutate` to the project's current state and persist it."""
    with project_lock(project_id):
        doc = load_project(project_id)
        mutate(doc)
        save_project(doc)
        return doc


def delete_project(project_id: str) -> None:
    target = project_dir(project_id)
    if not target.exists():
        raise HTTPException(status_code=404, detail="project not found")
    shutil.rmtree(target)


def list_projects() -> list[dict]:
    ensure_dirs()
    out = []
    for entry in PROJECTS_DIR.iterdir():
        if not entry.is_dir():
            continue
        doc = _read_json(entry / "project.json", None)
        if not doc:
            continue
        ctx = doc.get("context", {})
        vo = doc.get("voiceover", {})
        out.append(
            {
                "id": doc.get("id", entry.name),
                "topic": ctx.get("topic", ""),
                "category": ctx.get("category", ""),
                "language": ctx.get("language", ""),
                "minutes": ctx.get("minutes", 0),
                "selected_title": doc.get("selected_title", ""),
                "step": doc.get("step", 1),
                "scene_count": len(doc.get("scenes", [])),
                "has_script": bool(doc.get("script")),
                "voiceover_status": vo.get("status", "idle"),
                "created_at": doc.get("created_at", ""),
                "updated_at": doc.get("updated_at", ""),
            }
        )
    out.sort(key=lambda p: p.get("updated_at", ""), reverse=True)
    return out


# --- Niches -----------------------------------------------------------------


def niche_dir(niche_id: str) -> Path:
    """Resolve a niche id to its directory, rejecting anything else.

    Same positive-pattern check as `project_dir`, and for the same reason: a
    basename round-trip would let ".." through, and a niche directory holds an
    uploaded video and a folder of screenshots.
    """
    if not _SAFE_ID.fullmatch(niche_id or ""):
        raise HTTPException(status_code=400, detail="invalid niche id")
    path = NICHES_DIR / niche_id
    if path.resolve().parent != NICHES_DIR.resolve():
        raise HTTPException(status_code=400, detail="invalid niche id")
    return path


def shots_dir(niche_id: str) -> Path:
    return niche_dir(niche_id) / "shots"


def niche_video_dir(niche_id: str) -> Path:
    return niche_dir(niche_id) / "video"


def niche_clips_dir(niche_id: str) -> Path:
    return niche_video_dir(niche_id) / "clips"


def niche_path(niche_id: str) -> Path:
    return niche_dir(niche_id) / "niche.json"


def new_niche(name: str) -> dict:
    ensure_dirs()
    nid = f"{slugify(name)}-{uuid.uuid4().hex[:6]}"
    doc = {
        "id": nid,
        "name": name.strip(),
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "source": {
            "youtube_url": "",
            "title": "",
            "channel": "",
            "duration_sec": 0,
            "video_file": "",
            "downloaded_at": "",
        },
        "transcript": "",
        "shots": [],
        "notes": "",
        "analysis": {"status": "idle"},
        # Stays empty until an analysis run fills it. Everything downstream
        # treats an empty profile as "this niche has nothing to say yet".
        "profile": {},
    }
    shots_dir(nid).mkdir(parents=True, exist_ok=True)
    niche_clips_dir(nid).mkdir(parents=True, exist_ok=True)
    save_niche(doc)
    return doc


def save_niche(doc: dict) -> dict:
    doc["updated_at"] = now_iso()
    _write_json(niche_path(doc["id"]), doc)
    return doc


def load_niche(niche_id: str) -> dict:
    doc = _read_json(niche_path(niche_id), None)
    if doc is None:
        raise HTTPException(status_code=404, detail="niche not found")
    return doc


@contextmanager
def niche_lock(niche_id: str):
    with _entity_lock(f"niche:{niche_id}"):
        yield


def update_niche(niche_id: str, mutate: Callable[[dict], None]) -> dict:
    """Apply `mutate` to the niche's current state and persist it.

    The analysis job writes clip results from a background task while the
    browser can be PATCHing the name or notes, so the read-modify-write cycle
    needs the same serialization projects get.
    """
    with niche_lock(niche_id):
        doc = load_niche(niche_id)
        mutate(doc)
        save_niche(doc)
        return doc


def delete_niche(niche_id: str) -> None:
    target = niche_dir(niche_id)
    if not target.exists():
        raise HTTPException(status_code=404, detail="niche not found")
    shutil.rmtree(target)


def list_niches() -> list[dict]:
    ensure_dirs()
    out = []
    for entry in NICHES_DIR.iterdir():
        if not entry.is_dir():
            continue
        doc = _read_json(entry / "niche.json", None)
        if not doc:
            continue
        source = doc.get("source", {})
        out.append(
            {
                "id": doc.get("id", entry.name),
                "name": doc.get("name", ""),
                "status": doc.get("analysis", {}).get("status", "idle"),
                "has_profile": bool(doc.get("profile")),
                "has_transcript": bool(doc.get("transcript")),
                "has_video": bool(source.get("video_file")),
                "youtube_url": source.get("youtube_url", ""),
                "shot_count": len(doc.get("shots", [])),
                "created_at": doc.get("created_at", ""),
                "updated_at": doc.get("updated_at", ""),
            }
        )
    out.sort(key=lambda n: n.get("updated_at", ""), reverse=True)
    return out


# --- Voices -----------------------------------------------------------------


def load_voices() -> dict:
    return _read_json(VOICES_PATH, {})


def save_voices(voices: dict) -> None:
    _write_json(VOICES_PATH, voices)


def get_voice(voice_id: str) -> dict:
    voice = load_voices().get(voice_id)
    if not voice:
        raise HTTPException(status_code=404, detail="voice not found")
    return voice


def sample_path(voice: dict) -> Path:
    return SAMPLES_DIR / voice["filename"]


def delete_voice(voice_id: str) -> None:
    voices = load_voices()
    voice = voices.pop(voice_id, None)
    if voice is None:
        raise HTTPException(status_code=404, detail="voice not found")
    sample = sample_path(voice)
    if sample.exists():
        sample.unlink()
    save_voices(voices)


# --- Style presets ----------------------------------------------------------


def load_style_presets() -> dict:
    return _read_json(STYLE_PRESETS_PATH, {})
