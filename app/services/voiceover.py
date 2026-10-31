"""Cloned-voice narration.

One track for the whole script. The script is split on sentence boundaries into
chunks small enough for a single request, each chunk is synthesized, and the
PCM is concatenated into `voiceover_full.wav`.

Chunking on sentences rather than on scenes matters for how it sounds: the
model sees whole sentences, so the prosody carries. A ten-minute script is
about six requests instead of one per image.
"""

import asyncio
import base64
import time

from fastapi import HTTPException

from app.audio import concat_wavs, split_text, wav_duration_seconds
from app.config import (
    MAX_CHUNK_CHARS,
    MAX_WPM,
    MIN_WPM,
    VOICEOVER_CONCURRENCY,
    VOICEOVER_RETRIES,
)
from app.export import write_exports
from app.mimo import synth_clone
from app.storage import (
    audio_dir,
    get_voice,
    load_project,
    load_style_presets,
    now_iso,
    sample_path,
    save_project,
    update_project,
)

# Project ids with a job in flight, so a double click cannot start two.
_RUNNING: set[str] = set()

FULL_TRACK = "voiceover_full.wav"


def build_style_prompt(voice: dict) -> str:
    """Translate the Studio sliders into prose.

    MIMO's cloning model exposes no numeric knobs, so a natural-language style
    prompt is the only lever it responds to.
    """
    parts: list[str] = []

    preset_id = voice.get("preset_id")
    if preset_id:
        preset = load_style_presets().get(preset_id)
        if preset and preset.get("prompt"):
            parts.append(preset["prompt"].strip())

    category = (voice.get("category") or "").strip()
    if category:
        parts.append(f"Narrate in a {category} style.")

    description = (voice.get("description") or "").strip()
    if description:
        parts.append(description.rstrip(".") + ".")

    stability = float(voice.get("stability", 45))
    if stability < 34:
        parts.append("Expressive, dynamic delivery with varied intonation.")
    elif stability <= 66:
        parts.append("Balanced, natural intonation.")
    else:
        parts.append("Steady, consistent, even tone.")

    style = float(voice.get("style", 9))
    if style < 20:
        parts.append("Minimal stylization, natural read.")
    elif style <= 60:
        parts.append("Moderately stylized delivery.")
    else:
        parts.append("Strongly stylized, expressive delivery.")

    if float(voice.get("similarity", 75)) > 66:
        parts.append("Stay faithful to the reference speaker's timbre.")

    if voice.get("speaker_boost", True):
        parts.append("Clear, well-projected, present voice.")

    speed = float(voice.get("speed", 0.97))
    if speed < 0.95:
        parts.append("Speak at a slightly slower pace.")
    elif speed > 1.05:
        parts.append("Speak at a slightly faster pace.")
    else:
        parts.append("Speak at a natural pace.")

    return " ".join(parts)


def read_sample_b64(voice: dict) -> str:
    path = sample_path(voice)
    if not path.exists():
        raise HTTPException(status_code=410, detail="voice sample missing on disk")
    return base64.b64encode(path.read_bytes()).decode("ascii")


def synthesize(text: str, sample_b64: str, mime: str, style_prompt: str) -> bytes:
    """Synthesize text, chunking anything long enough to worry about."""
    chunks = split_text(text, MAX_CHUNK_CHARS)
    if not chunks:
        return b""
    if len(chunks) == 1:
        return synth_clone(chunks[0], sample_b64, mime, style_prompt)
    return concat_wavs([synth_clone(c, sample_b64, mime, style_prompt) for c in chunks])


def chunks_dir(project_id: str):
    """Where the per-request pieces live, so a retry does not redo the lot."""
    return audio_dir(project_id) / "chunks"


def measured_wpm(words: int, seconds: float) -> int:
    """The pace the model actually spoke at.

    The project's `wpm` setting decides how many words go into each image's
    slot, and 150 is only right for English at a neutral pace. A cloned voice
    reading another language, or one carrying a "slower pace" style prompt, can
    come in far below that. Clamped to the range the API accepts back, because
    the UI offers this number as a one-click replacement for the setting.
    """
    if not seconds or not words:
        return 0
    return max(MIN_WPM, min(MAX_WPM, round(words / seconds * 60)))


async def _synthesize_chunk(
    index: int,
    text: str,
    sample_b64: str,
    mime: str,
    style_prompt: str,
    out_dir,
    semaphore: asyncio.Semaphore,
) -> tuple[int, bool, str]:
    async with semaphore:
        last_error = ""
        for attempt in range(VOICEOVER_RETRIES + 1):
            try:
                wav = await asyncio.to_thread(
                    synth_clone, text, sample_b64, mime, style_prompt
                )
            except Exception as e:  # noqa: BLE001 - reported per chunk, never fatal
                last_error = str(getattr(e, "detail", e))[:200]
                if attempt < VOICEOVER_RETRIES:
                    await asyncio.sleep(2**attempt)
                continue
            if not wav:
                last_error = "no audio returned"
                continue
            (out_dir / f"chunk_{index:03d}.wav").write_bytes(wav)
            return index, True, ""
        return index, False, last_error or "synthesis failed"


def _build_full_track(project_id: str, count: int) -> tuple[str, float, list[int]]:
    """Concatenate the rendered chunks in order. Returns (name, seconds, gaps)."""
    pieces = chunks_dir(project_id)
    wavs = []
    gaps = []
    for index in range(1, count + 1):
        path = pieces / f"chunk_{index:03d}.wav"
        if path.exists():
            wavs.append(path.read_bytes())
        else:
            gaps.append(index)
    if not wavs:
        return "", 0.0, gaps
    full = concat_wavs(wavs)
    (audio_dir(project_id) / FULL_TRACK).write_bytes(full)
    return FULL_TRACK, wav_duration_seconds(full), gaps


def _retime_scenes(doc: dict, wpm: int) -> None:
    """Re-lay the shot list against the pace the voice actually read at.

    The narration is one continuous track, so the images only stay in sync if
    their durations add up to it. Once the real pace is known, every scene's
    start and duration are recomputed from it.
    """
    if not wpm:
        return
    clock = 0.0
    for scene in doc.get("scenes", []):
        words = len((scene.get("narration") or "").split())
        actual = round(words / wpm * 60, 1) if words else 0.0
        scene["actual_sec"] = actual
        scene["start_sec"] = round(clock, 1)
        scene["duration_sec"] = actual
        clock += actual


def _save(project_id: str, state: dict, wpm: int = 0) -> dict:
    """Persist only what this job owns, on top of whatever is on disk now.

    The job runs for minutes while the creator keeps using the app. Holding the
    document it loaded at the start and writing that back would erase every
    edit made in the meantime.
    """

    def mutate(doc: dict) -> None:
        doc["voiceover"] = state
        if wpm:
            _retime_scenes(doc, wpm)

    return update_project(project_id, mutate)


async def run_job(project_id: str, voice_id: str, retry_only: bool = False) -> None:
    """Render the whole script as one track, in the background."""
    if project_id in _RUNNING:
        return
    _RUNNING.add(project_id)
    lock = asyncio.Lock()
    started = time.time()

    try:
        doc = load_project(project_id)
        script = (doc.get("script") or "").strip()
        if not script:
            raise HTTPException(status_code=400, detail="there is no script to narrate")

        voice = get_voice(voice_id)
        sample_b64 = read_sample_b64(voice)
        style_prompt = build_style_prompt(voice)
        out_dir = chunks_dir(project_id)
        out_dir.mkdir(parents=True, exist_ok=True)

        chunks = split_text(script, MAX_CHUNK_CHARS)
        previous = doc.get("voiceover", {})
        pending = list(range(1, len(chunks) + 1))
        if retry_only:
            # Only the pieces that are missing from disk.
            pending = [i for i in pending if not (out_dir / f"chunk_{i:03d}.wav").exists()]

        state = {
            "status": "running",
            "voice_id": voice_id,
            "voice_name": voice.get("name", ""),
            "total": len(chunks),
            "done": len(chunks) - len(pending),
            "failed": [],
            "errors": {},
            "started_at": now_iso(),
            "finished_at": "",
            "full_file": previous.get("full_file", ""),
            "full_sec": previous.get("full_sec", 0.0),
        }
        _save(project_id, state)

        semaphore = asyncio.Semaphore(VOICEOVER_CONCURRENCY)

        async def worker(index: int) -> None:
            _, ok, error = await _synthesize_chunk(
                index, chunks[index - 1], sample_b64, voice["mime"], style_prompt,
                out_dir, semaphore,
            )
            async with lock:
                if ok:
                    state["done"] += 1
                    if index in state["failed"]:
                        state["failed"].remove(index)
                    state["errors"].pop(str(index), None)
                else:
                    if index not in state["failed"]:
                        state["failed"].append(index)
                    state["errors"][str(index)] = error
                # Persist as we go so a refresh or restart never loses progress.
                _save(project_id, state)

        await asyncio.gather(*(worker(i) for i in pending))

        full_file, full_sec, gaps = await asyncio.to_thread(
            _build_full_track, project_id, len(chunks)
        )
        pace = measured_wpm(len(script.split()), full_sec)

        state["full_file"] = full_file
        state["full_sec"] = full_sec
        state["measured_wpm"] = pace
        state["failed"] = sorted(set(state["failed"]) | set(gaps))
        # A track missing a chunk has a silent hole in the middle of a
        # sentence. It is still written, because most of it is usable, but the
        # gap is named rather than hidden.
        state["status"] = "partial" if state["failed"] else "done"
        state["finished_at"] = now_iso()
        state["elapsed_sec"] = round(time.time() - started, 1)

        doc = _save(project_id, state, pace)
        doc["step"] = max(doc.get("step", 1), 5)
        save_project(doc)
        # The scenes now carry measured speaking times, so the CSV on disk is
        # stale until it is rewritten.
        await asyncio.to_thread(write_exports, doc)

    except Exception as e:  # noqa: BLE001 - the job must never crash silently
        try:
            doc = load_project(project_id)
            failed_state = doc.get("voiceover", {})
            failed_state["status"] = "error"
            failed_state["error"] = str(getattr(e, "detail", e))[:300]
            failed_state["finished_at"] = now_iso()
            doc["voiceover"] = failed_state
            save_project(doc)
        except Exception:
            pass
    finally:
        _RUNNING.discard(project_id)


def is_running(project_id: str) -> bool:
    return project_id in _RUNNING
