"""Turning a reference — video, screenshots, transcript — into one small profile.

A ten-minute transcript is around 1,500 words, and the script prompt already
carries its own word budget. Dropping the raw transcript beside those
instructions would drown them, so nothing raw ever reaches a project: this
module compresses everything into the compact profile in `EMPTY_PROFILE`, and
only that is injected downstream.
"""

import asyncio
import json

from fastapi import HTTPException

from app import media
from app.config import (
    NICHE_ANALYSIS_CONCURRENCY,
    NICHE_CLIP_ATTEMPTS,
    NICHE_CLIP_COUNT,
    NICHE_CLIP_SECONDS,
)
from app.mimo import chat_json, chat_json_parts, image_part, video_part
from app.storage import (
    load_niche,
    niche_clips_dir,
    niche_video_dir,
    now_iso,
    save_niche,
    shots_dir,
    update_niche,
)

# The profile's fields, split by the shape each one holds. Anything the model
# returns outside this set is dropped: the prompt builders downstream read
# fixed keys, and a hallucinated field would be silently ignored anyway.
_LIST_FIELDS = ("hook_patterns", "vocabulary", "structure", "shot_language", "avoid")
_TEXT_FIELDS = ("tone", "pacing", "title_style", "visual_style", "image_prompt_seed")

EMPTY_PROFILE: dict = {
    **{field: "" for field in _TEXT_FIELDS},
    **{field: [] for field in _LIST_FIELDS},
}

_MAX_LIST_ITEMS = 12
_MAX_TEXT_CHARS = 600

# Asked about a field the material cannot answer, the model writes a sentence
# saying so rather than leaving it blank — observed live as "Not specified in
# the material." for title style, from a video with no title on screen. Left
# alone that sentence is handed to the title writer as instruction.
_NON_ANSWERS = {
    "n/a",
    "na",
    "none",
    "nothing",
    "unknown",
    "unspecified",
    "undetermined",
    "not specified",
    "not applicable",
    "not available",
    "not determined",
}
# Matched as prefixes so the trailing clause ("...in the material") does not
# have to be enumerated. Whole-value only: a real answer that merely contains
# the word "none" must survive.
_NON_ANSWER_PREFIXES = (
    "not specified",
    "not applicable",
    "not available",
    "not stated",
    "not shown",
    "not visible",
    "not mentioned",
    "not determinable",
    "not enough information",
    "no information",
    "cannot be determined",
    "unable to determine",
)


def _is_non_answer(value: str) -> bool:
    cleaned = value.strip().strip(".!").lower()
    if not cleaned:
        return True
    if cleaned in _NON_ANSWERS:
        return True
    return cleaned.startswith(_NON_ANSWER_PREFIXES)

MERGE_SYSTEM = (
    "You study a reference channel and describe how it works, so another writer can "
    "match it. You are specific and concrete: name the devices, quote the habits, and "
    "say what the reference avoids as well as what it does. You never invent detail "
    "that the material does not support. "
    "Output ONLY a valid JSON object, no markdown, no commentary."
)


def plan_clips(
    duration: float,
    clip_seconds: int = NICHE_CLIP_SECONDS,
    max_clips: int = NICHE_CLIP_COUNT,
) -> list[tuple[float, float]]:
    """Pick which stretches of a reference video to analyse.

    Returns (start, length) pairs in seconds. The count is floored by
    `duration // clip_seconds` rather than fixed: at a fixed four, a
    two-minute video would be sampled at 0s, 30s, 60s and 90s, and four
    45-second clips taken from those offsets would overlap almost entirely —
    four times the token cost for nearly the same footage.
    """
    if duration <= 0:
        return []
    if duration <= clip_seconds:
        # Shorter than one clip: use it whole rather than padding to the cap.
        return [(0.0, float(duration))]

    count = max(1, min(max_clips, int(duration // clip_seconds)))
    # Spreading by `duration * i / count` keeps the last clip inside the video:
    # its start is `duration * (count - 1) / count`, and `count <= duration /
    # clip_seconds` guarantees `clip_seconds <= duration / count`.
    return [(round(duration * i / count, 3), float(clip_seconds)) for i in range(count)]


_LABELS = {
    "hook_patterns": "How it hooks",
    "tone": "Narration tone",
    "pacing": "Pacing",
    "vocabulary": "Words it returns to",
    "structure": "How it is ordered",
    "title_style": "How it packages a title",
    "visual_style": "Visual style",
    "shot_language": "Framing habits",
    "avoid": "What it never does",
    "image_prompt_seed": "Art direction",
}

# Which slice of the profile each consumer gets. A title writer has no use for
# art direction, and handing it over would only dilute the ask.
TITLE_FIELDS = ("title_style", "hook_patterns")
SCRIPT_FIELDS = ("tone", "pacing", "vocabulary", "structure", "hook_patterns")
VISUAL_FIELDS = ("visual_style", "shot_language", "image_prompt_seed", "avoid")


def reference_block(profile, fields: tuple[str, ...]) -> str:
    """Render the given profile fields as prompt text, or "" if there are none.

    Returning an empty string for an empty profile is what keeps the feature
    inert: a project with no niche must build exactly the prompt it always did.
    """
    if not isinstance(profile, dict) or not profile:
        return ""

    lines = []
    for field in fields:
        value = profile.get(field)
        if isinstance(value, list):
            value = "; ".join(str(v).strip() for v in value if str(v).strip())
        value = str(value or "").strip()
        if value:
            lines.append(f"- {_LABELS.get(field, field)}: {value}")

    if not lines:
        return ""
    return "The creator picked a reference to match. Echo it:\n" + "\n".join(lines)


def normalise_profile(raw) -> dict:
    """Coerce whatever the model returned into the fixed profile shape.

    Models are inconsistent about singular versus plural: a field asked for as
    a list comes back as a bare string often enough that treating it as a
    failure would throw away good analysis.
    """
    profile = {**EMPTY_PROFILE, **{field: [] for field in _LIST_FIELDS}}
    if not isinstance(raw, dict):
        return profile

    for field in _TEXT_FIELDS:
        value = raw.get(field)
        if isinstance(value, list):
            value = " ".join(str(v).strip() for v in value if str(v).strip())
        text = str(value or "").strip()[:_MAX_TEXT_CHARS]
        profile[field] = "" if _is_non_answer(text) else text

    for field in _LIST_FIELDS:
        value = raw.get(field) or []
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            value = []
        profile[field] = [
            str(v).strip()
            for v in value
            if v is not None and not _is_non_answer(str(v))
        ][:_MAX_LIST_ITEMS]

    return profile


def join_transcript(clip_results: list) -> str:
    """Stitch the per-clip transcript slices back into reading order.

    Clips are analysed concurrently so they finish out of order, and a clip
    that failed every attempt is None. Sorting on the clip's start time keeps
    the transcript readable and lets a partial run still produce something
    useful rather than nothing.
    """
    usable = [r for r in clip_results if isinstance(r, dict) and r.get("transcript")]
    usable.sort(key=lambda r: float(r.get("start") or 0))
    return "\n\n".join(str(r["transcript"]).strip() for r in usable)


def build_merge_prompt(clip_results: list, shots_result, pasted_transcript: str) -> str:
    """Lay the raw observations out for the reconciling call."""
    sections = []

    usable = [r for r in clip_results if isinstance(r, dict)]
    usable.sort(key=lambda r: float(r.get("start") or 0))
    for result in usable:
        start = float(result.get("start") or 0)
        observation = {k: v for k, v in result.items() if k not in ("start", "transcript")}
        sections.append(
            f"Observations from the clip at {int(start // 60):02d}:{int(start % 60):02d}:\n"
            f"{json.dumps(observation, ensure_ascii=False)}"
        )

    if isinstance(shots_result, dict) and shots_result:
        sections.append(
            f"Observations from the creator's screenshots:\n"
            f"{json.dumps(shots_result, ensure_ascii=False)}"
        )

    transcript = (pasted_transcript or "").strip()
    if transcript:
        # Truncated deliberately: this is the merge call's evidence, not the
        # payload, and a full hour of narration would crowd out the clip
        # observations it is supposed to be reconciled against.
        sections.append(f"Transcript supplied by the creator:\n{transcript[:12000]}")

    fields = (
        '  "hook_patterns": ["how it opens and re-opens curiosity"],\n'
        '  "tone": "the narrator\'s register and attitude",\n'
        '  "pacing": "sentence length and rhythm",\n'
        '  "vocabulary": ["words and turns of phrase it returns to"],\n'
        '  "structure": ["how the video is ordered, beat by beat"],\n'
        '  "title_style": "how this channel packages a title",\n'
        '  "visual_style": "palette, lighting, camera language",\n'
        '  "shot_language": ["framing habits"],\n'
        '  "avoid": ["what this reference conspicuously never does"],\n'
        '  "image_prompt_seed": "one reusable art-direction line for stills in this style"\n'
    )
    return (
        "\n\n".join(sections)
        + "\n\nReconcile the material above into one profile of this reference.\n"
        "Where the observations disagree, prefer what appears in more than one of them.\n"
        "If the material cannot support a field, return an empty string or an empty "
        'list for it. Do not write "not specified", "unknown" or any other sentence '
        "saying you could not tell — an empty value is the correct answer there.\n"
        "The exception is image_prompt_seed: it is derived, not observed. Whenever "
        "you have written anything for visual_style, condense it into that one line "
        "as well. Leave it empty only if you saw no imagery at all.\n"
        "Return JSON:\n{\n" + fields + "}"
    )


def merge_partials(clip_results: list, shots_result, pasted_transcript: str) -> dict:
    """Reconcile every observation into the one profile projects will read.

    With nothing to reconcile the model is not called at all: an empty profile
    is the honest answer, and asking anyway would invite invention.
    """
    has_clips = any(isinstance(r, dict) for r in clip_results)
    has_shots = isinstance(shots_result, dict) and bool(shots_result)
    has_text = bool((pasted_transcript or "").strip())
    if not (has_clips or has_shots or has_text):
        return dict(EMPTY_PROFILE)

    raw = chat_json(
        MERGE_SYSTEM,
        build_merge_prompt(clip_results, shots_result, pasted_transcript),
        temperature=0.4,
        max_tokens=4000,
    )
    return normalise_profile(raw)


# --- The analysis job -------------------------------------------------------

CLIP_SYSTEM = (
    "You study one excerpt of a reference video and report what you observe. You watch "
    "the picture and you listen to the narration. You describe only what is actually "
    "there — no invention, no filler. "
    "Output ONLY a valid JSON object, no markdown, no commentary."
)

SHOTS_SYSTEM = (
    "You study a set of stills from a reference channel and describe their shared "
    "visual system: palette, lighting, framing, subject treatment, and what they never "
    "show. "
    "Output ONLY a valid JSON object, no markdown, no commentary."
)

CLIP_ASK = (
    "Return JSON:\n"
    "{\n"
    '  "transcript": "the narration in this excerpt, verbatim",\n'
    '  "tone": "the narrator\'s register and attitude",\n'
    '  "pacing": "sentence length and rhythm",\n'
    '  "hook_patterns": ["devices used to open or re-open curiosity"],\n'
    '  "vocabulary": ["words and turns of phrase it leans on"],\n'
    '  "structure": ["what happens, in order, in this excerpt"],\n'
    '  "visual_style": "palette, lighting, camera language",\n'
    '  "shot_language": ["how each shot is framed"],\n'
    '  "avoid": ["what this excerpt conspicuously never does"]\n'
    "}"
)

SHOTS_ASK = (
    "Return JSON:\n"
    "{\n"
    '  "visual_style": "palette, lighting, camera language shared by these stills",\n'
    '  "shot_language": ["framing habits"],\n'
    '  "avoid": ["what these stills never show"],\n'
    '  "image_prompt_seed": "one reusable art-direction line for stills in this style"\n'
    "}"
)

_RUNNING: set[str] = set()


def is_running(niche_id: str) -> bool:
    return niche_id in _RUNNING


def _timecode(seconds: float) -> str:
    total = int(max(0.0, seconds))
    return f"{total // 60:02d}:{total % 60:02d}"


def _range_label(start: float, length: float) -> str:
    return f"{_timecode(start)}-{_timecode(start + length)}"


def analyse_clip(raw: bytes, start: float, length: float) -> dict:
    """One clip, one call. The model reads its frames and its audio together."""
    result = chat_json_parts(
        CLIP_SYSTEM,
        [
            {
                "type": "text",
                "text": (
                    f"This is the excerpt at {_range_label(start, length)} of a "
                    f"reference video.\n\n{CLIP_ASK}"
                ),
            },
            video_part(raw),
        ],
        max_tokens=3000,
    )
    return {**result, "start": start, "length": length}


def analyse_shots(images: list[tuple[bytes, str]]) -> dict:
    """Every screenshot in one call: their shared system is the thing of value."""
    parts: list[dict] = [{"type": "text", "text": SHOTS_ASK}]
    parts.extend(image_part(raw, mime) for raw, mime in images)
    return chat_json_parts(SHOTS_SYSTEM, parts, max_tokens=2000)


def _save_state(niche_id: str, state: dict, **fields) -> dict:
    """Persist what this job owns on top of whatever is on disk now.

    The job runs for minutes while the creator keeps editing the niche, so
    writing back the document loaded at the start would erase those edits.
    """

    def mutate(doc: dict) -> None:
        doc["analysis"] = state
        for key, value in fields.items():
            if key == "source":
                doc["source"] = {**doc.get("source", {}), **value}
            else:
                doc[key] = value

    return update_niche(niche_id, mutate)


def _load_shots(doc: dict) -> list[tuple[bytes, str]]:
    directory = shots_dir(doc["id"])
    images = []
    for shot in doc.get("shots", []):
        path = directory / str(shot.get("filename", ""))
        if path.is_file():
            images.append((path.read_bytes(), shot.get("mime", "image/png")))
    return images


async def _analyse_one(
    path, start: float, length: float, semaphore: asyncio.Semaphore
) -> dict | None:
    """Analyse a single clip, retrying once. None means it never answered."""
    raw = path.read_bytes()
    async with semaphore:
        for attempt in range(1, NICHE_CLIP_ATTEMPTS + 1):
            try:
                return await asyncio.to_thread(analyse_clip, raw, start, length)
            except Exception:  # noqa: BLE001 — reported per clip, never fatal
                if attempt < NICHE_CLIP_ATTEMPTS:
                    await asyncio.sleep(2**attempt)
    return None


async def run_job(niche_id: str) -> None:
    """Analyse a niche's reference material in the background."""
    if niche_id in _RUNNING:
        return
    _RUNNING.add(niche_id)

    state = {"status": "running", "started_at": now_iso(), "finished_at": ""}
    try:
        _save_state(niche_id, state)
        doc = load_niche(niche_id)
        source = dict(doc.get("source", {}))
        video_path = None

        if source.get("video_file"):
            candidate = niche_video_dir(niche_id) / source["video_file"]
            if candidate.is_file():
                video_path = candidate

        if video_path is None and source.get("youtube_url"):
            target = niche_video_dir(niche_id)
            target.mkdir(parents=True, exist_ok=True)
            info = await asyncio.to_thread(
                media.download_youtube, source["youtube_url"], str(target)
            )
            from pathlib import Path

            video_path = Path(info["path"])
            source.update(
                {
                    "title": info["title"],
                    "channel": info["channel"],
                    "duration_sec": info["duration_sec"],
                    "video_file": video_path.name,
                    "downloaded_at": now_iso(),
                }
            )
            state["stage"] = "downloaded"
            _save_state(niche_id, state, source=source)

        clip_results: list = []
        failed_ranges: list[str] = []

        if video_path is not None:
            duration = float(source.get("duration_sec") or 0) or await asyncio.to_thread(
                media.probe_duration, str(video_path)
            )
            source["duration_sec"] = duration
            plan = plan_clips(duration)
            clips_dir = niche_clips_dir(niche_id)
            clips_dir.mkdir(parents=True, exist_ok=True)

            cut = []
            for index, (start, length) in enumerate(plan, start=1):
                dest = clips_dir / f"clip_{index:02d}.mp4"
                cut.append(
                    (
                        await asyncio.to_thread(
                            media.cut_clip, str(video_path), start, length, str(dest)
                        ),
                        start,
                        length,
                    )
                )

            state["stage"] = "analysing"
            state["total_clips"] = len(cut)
            _save_state(niche_id, state, source=source)

            semaphore = asyncio.Semaphore(NICHE_ANALYSIS_CONCURRENCY)
            clip_results = list(
                await asyncio.gather(
                    *(_analyse_one(path, start, length, semaphore) for path, start, length in cut)
                )
            )
            failed_ranges = [
                _range_label(start, length)
                for (_, start, length), result in zip(cut, clip_results)
                if result is None
            ]

        shots_result = None
        images = _load_shots(doc)
        if images:
            try:
                shots_result = await asyncio.to_thread(analyse_shots, images)
            except Exception:  # noqa: BLE001 — the clips can still carry the run
                failed_ranges.append("screenshots")

        pasted = (doc.get("transcript") or "").strip()
        clip_transcript = join_transcript(clip_results)

        usable = any(isinstance(r, dict) for r in clip_results)
        if not usable and shots_result is None and not pasted:
            raise HTTPException(
                status_code=400,
                detail=(
                    "There is nothing to analyse. Add a YouTube link, a video, "
                    "screenshots, or paste a transcript first."
                ),
            )

        profile = await asyncio.to_thread(
            merge_partials, clip_results, shots_result, pasted or clip_transcript
        )

        state.update(
            {
                # A hole in the middle of the reference is worth saying out
                # loud rather than shipping quietly as if it were complete.
                "status": "partial" if failed_ranges else "done",
                "failed_ranges": failed_ranges,
                "finished_at": now_iso(),
                "stage": "",
            }
        )
        fields = {"profile": profile, "source": source}
        if clip_transcript and not pasted:
            fields["transcript"] = clip_transcript
        _save_state(niche_id, state, **fields)

    except Exception as e:  # noqa: BLE001 — the job owns its own failure reporting
        state.update(
            {
                "status": "failed",
                "error": str(getattr(e, "detail", e))[:300],
                "finished_at": now_iso(),
                "stage": "",
            }
        )
        try:
            _save_state(niche_id, state)
        except Exception:  # noqa: BLE001 — the niche may have been deleted mid-run
            pass
    finally:
        _RUNNING.discard(niche_id)
