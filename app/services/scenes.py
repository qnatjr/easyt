"""Scene breakdown and image-prompt generation.

One scene is one still image held on screen for a few seconds. A ten-minute
video at three seconds a scene is 200 images, so no single LLM call can produce
them: the work is split into four stages — the script is segmented by code, a
style bible is generated once, the prompts are generated in batches, and the
thumbnail is generated on its own.

Segmenting in code rather than asking the model to do it guarantees three
things the model cannot: every word of the script is covered, the scene count
is exact, and the order is preserved.
"""

import asyncio

from app.config import (
    BASE_NEGATIVE_PROMPT,
    DEFAULT_WPM,
    MAX_SCENES,
    SCENE_BATCH_ATTEMPTS,
    SCENE_BATCH_CONCURRENCY,
    SCENE_BATCH_SIZE,
    SCENE_SECONDS,
    SHOT_CYCLE,
)
from app.mimo import chat_json
from app.services.niche import VISUAL_FIELDS, reference_block

_CLAUSE_ENDINGS = (",", ";", ":", "—", "–", "--")

BIBLE_SYSTEM = (
    "You are an art director for AI-generated imagery. You define a visual system that "
    "keeps hundreds of independently generated stills looking like one film. "
    "Output ONLY a valid JSON object, no markdown, no commentary."
)

PROMPT_SYSTEM = (
    "You are a prompt engineer for AI image generation. You write dense, concrete, "
    "visual prompts: subject first, then action, setting, composition, lighting, lens "
    "and style. You never ask for text or lettering in an image, because generative "
    "models render it badly. You never pad with vague adjectives. "
    "Output ONLY a valid JSON object, no markdown, no commentary."
)

THUMBNAIL_SYSTEM = (
    "You design YouTube thumbnails that win the click at 200 pixels wide. "
    "Output ONLY a valid JSON object, no markdown, no commentary."
)


def words_per_scene(wpm: int = DEFAULT_WPM, seconds: int = SCENE_SECONDS) -> int:
    """How many spoken words fit in one scene at the given pace.

    Deliberately floored, not rounded. Rounding to nearest would let the budget
    exceed the scene, and the error would compound across hundreds of images.
    """
    return max(2, int(wpm / 60 * seconds))


def shot_for(index: int) -> str:
    """The shot type for scene `index`, cycling so the montage has rhythm."""
    return SHOT_CYCLE[(index - 1) % len(SHOT_CYCLE)]


def _split_long_sentence(words: list[str], target: int) -> list[list[str]]:
    """Break a sentence longer than one scene, preferring clause boundaries.

    No piece may exceed `target`: a scene's narration has to fit the seconds
    its image is on screen.
    """
    pieces: list[list[str]] = []
    cur: list[str] = []
    soft = max(2, int(target * 0.7))
    for word in words:
        cur.append(word)
        at_clause = word.rstrip("\"'").endswith(_CLAUSE_ENDINGS)
        if len(cur) >= target or (at_clause and len(cur) >= soft):
            pieces.append(cur)
            cur = []
    if cur:
        if pieces and len(pieces[-1]) + len(cur) <= target:
            pieces[-1].extend(cur)
        else:
            pieces.append(cur)
    return pieces


def segment_script(script: str, target: int) -> list[str]:
    """Split a script into segments of at most `target` words each."""
    return [beat["text"] for beat in segment_beats(script, target)]


def _absorb_stubs(beats: list[dict], target: int) -> list[dict]:
    """Fold one- and two-word beats into a neighbour.

    A stub beat is worse than a slightly long one. It buys a whole image for a
    word like "hari." and, because durations follow the narration, it also
    makes the cut rhythm lurch. Merging is allowed to overshoot the target
    because overshooting by a word is the lesser problem.
    """
    floor_words = max(1, int(target * 0.4))
    ceiling = max(target + 1, int(target * 1.5))
    merged: list[dict] = []

    for beat in beats:
        words = len(beat["text"].split())
        if merged and words < floor_words:
            previous = merged[-1]
            if len(previous["text"].split()) + words <= ceiling:
                previous["text"] = f"{previous['text']} {beat['text']}"
                if beat["context"] and beat["context"] not in previous["context"]:
                    previous["context"] = f"{previous['context']} {beat['context']}".strip()
                continue
        merged.append(beat)

    # A stub that opens the script has no previous beat, so it borrows the next.
    if len(merged) > 1 and len(merged[0]["text"].split()) < floor_words:
        head, following = merged[0], merged[1]
        if len(head["text"].split()) + len(following["text"].split()) <= ceiling:
            following["text"] = f"{head['text']} {following['text']}"
            if head["context"] and head["context"] not in following["context"]:
                following["context"] = f"{head['context']} {following['context']}".strip()
            merged.pop(0)

    return merged


def segment_beats(script: str, target: int) -> list[dict]:
    """Split a script into timed beats, each carrying its sentence for context.

    At three seconds a beat is only a handful of words — often half a clause,
    which is useless on its own as the basis for an image prompt. So every beat
    also carries the whole sentence it came from. The beat decides *when* the
    image appears; the sentence tells the model what the moment is *about*.
    """
    from app.audio import split_sentences

    script = (script or "").strip()
    if not script:
        return []

    beats: list[dict] = []
    cur: list[str] = []
    cur_sentences: list[str] = []

    def flush() -> None:
        if not cur:
            return
        beats.append({"text": " ".join(cur), "context": " ".join(cur_sentences).strip()})
        cur.clear()
        cur_sentences.clear()

    for sentence in split_sentences(script):
        words = sentence.split()
        if not words:
            continue
        if len(words) > target:
            flush()
            for piece in _split_long_sentence(words, target):
                beats.append({"text": " ".join(piece), "context": sentence})
            continue
        if cur and len(cur) + len(words) > target:
            flush()
        cur.extend(words)
        if sentence not in cur_sentences:
            cur_sentences.append(sentence)
        if len(cur) >= target:
            flush()

    if cur:
        tail = " ".join(cur)
        tail_context = " ".join(cur_sentences).strip()
        # Fold a stub tail into the previous beat only if it still fits.
        if beats and len(beats[-1]["text"].split()) + len(cur) <= target:
            beats[-1]["text"] = f"{beats[-1]['text']} {tail}"
            if tail_context and tail_context not in beats[-1]["context"]:
                beats[-1]["context"] = f"{beats[-1]['context']} {tail_context}".strip()
        else:
            beats.append({"text": tail, "context": tail_context})
        cur.clear()
        cur_sentences.clear()

    # Truncation is the caller's decision to report, not something to hide here.
    return _absorb_stubs(beats, target)


def estimate_seconds(narration: str, wpm: int) -> float:
    """How long this narration should take to speak, at the project's pace."""
    words = len(narration.split())
    return round(words / wpm * 60, 1) if wpm else 0.0


# --- Style bible ------------------------------------------------------------


def build_bible_prompt(ctx: dict, title: str, script: str) -> str:
    excerpt = script[:2500]
    niche = reference_block(ctx.get("niche_profile"), VISUAL_FIELDS)
    niche_block = f"{niche}\n\n" if niche else ""
    return (
        f"Video title: {title}\n"
        f"Topic: {ctx.get('topic', '')}\n"
        f"Category: {ctx.get('category', 'educational')}\n"
        f"{niche_block}"
        f"Script excerpt:\n{excerpt}\n\n"
        "Define the visual system for this video as JSON:\n"
        "{\n"
        '  "art_direction": "one paragraph: medium, rendering style, era, mood",\n'
        '  "palette": "the colour language in one sentence",\n'
        '  "lighting": "the lighting language in one sentence",\n'
        '  "camera": "lens and depth-of-field language in one sentence",\n'
        '  "recurring_subjects": ["fixed physical descriptions for people, places or '
        'objects that appear more than once, so they look identical across images"],\n'
        '  "avoid": ["things that must never appear: anachronisms, wrong architecture, '
        'modern objects in a historical scene, and so on"]\n'
        "}\n\n"
        "Write it in English. Be specific and physical — no vague adjectives. "
        "This text is pasted into every single image prompt, so keep it under "
        "150 words in total."
    )


def _bible_to_text(raw: dict) -> str:
    if not isinstance(raw, dict):
        return ""
    parts = []
    for key in ("art_direction", "palette", "lighting", "camera"):
        value = str(raw.get(key, "")).strip()
        if value:
            parts.append(f"{key.replace('_', ' ').title()}: {value}")
    subjects = raw.get("recurring_subjects") or []
    if isinstance(subjects, str):
        subjects = [subjects]
    if subjects:
        parts.append("Recurring subjects: " + "; ".join(str(s).strip() for s in subjects))
    return "\n".join(parts)


def _negative_prompt(raw: dict) -> str:
    """One negative prompt for the whole video.

    Global rather than per-scene: consistency matters more than variety here,
    and it saves a field on every one of a few hundred prompts.
    """
    avoid = raw.get("avoid") if isinstance(raw, dict) else None
    if isinstance(avoid, str):
        avoid = [avoid]
    extra = [str(a).strip() for a in (avoid or []) if str(a).strip()]
    return ", ".join([*extra, BASE_NEGATIVE_PROMPT]) if extra else BASE_NEGATIVE_PROMPT


def generate_style_bible(ctx: dict, title: str, script: str) -> tuple[str, str]:
    """Return (style bible text, negative prompt)."""
    raw = chat_json(
        BIBLE_SYSTEM, build_bible_prompt(ctx, title, script), temperature=0.7, max_tokens=1200
    )
    return _bible_to_text(raw), _negative_prompt(raw)


# --- Scene prompts ----------------------------------------------------------


def build_batch_prompt(
    batch: list[dict], bible: str, ctx: dict, previous_tail: str, seconds: int
) -> str:
    listing = "\n".join(
        f'{b["index"]}. [{b["shot"]}] moment: "{b["text"]}"'
        + (f'\n   (from the line: "{b["context"]}")' if b["context"] != b["text"] else "")
        for b in batch
    )
    continuity = (
        f"\nThe image immediately before this batch showed: {previous_tail}\n"
        if previous_tail
        else ""
    )
    return (
        f"Video topic: {ctx.get('topic', '')}\n"
        f"Category: {ctx.get('category', 'educational')}\n\n"
        f"VISUAL SYSTEM (obey this in every prompt):\n{bible}\n"
        f"{continuity}\n"
        f"Each entry below is one still image held on screen for {seconds} seconds while "
        f"that fragment of narration is spoken. The bracketed shot type is assigned — "
        f"honour it, it is what gives the montage its rhythm.\n\n"
        f"{listing}\n\n"
        "Return JSON:\n"
        "{\n"
        '  "scenes": [\n'
        "    {\n"
        '      "index": <the number given above>,\n'
        '      "image_prompt": "the full prompt: subject and what it is doing, setting, '
        "the assigned shot type and composition, lighting, lens and depth of field, "
        'art style, 16:9. One sentence-fragment list, comma separated, no prose.",\n'
        '      "focus": "two or three words naming what the viewer looks at"\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "Rules:\n"
        "- Write every prompt in English regardless of the narration language.\n"
        "- Visualize what the narration means; do not illustrate the words literally, "
        "and never render the words themselves.\n"
        "- One subject per image. A still cannot carry a montage.\n"
        "- Consecutive images should feel like cuts within one scene, not unrelated "
        "pictures: keep the location and subject continuous unless the narration moves on.\n"
        "- Keep recurring subjects described identically to the visual system.\n"
        "- Return one object per index given, no more, no fewer."
    )


def _normalize_batch(raw: dict, batch: list[dict]) -> dict[int, dict]:
    items = raw.get("scenes") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return {}
    valid = {b["index"] for b in batch}
    out: dict[int, dict] = {}
    leftovers: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        image = str(item.get("image_prompt", "")).strip()
        # The image prompt is the deliverable. An entry without one would be
        # marked "ok" and leave a blank cell the retry path never revisits.
        if not image:
            continue
        entry = {"image_prompt": image, "focus": str(item.get("focus", "")).strip()}
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            index = -1
        if index in valid and index not in out:
            out[index] = entry
        else:
            leftovers.append(entry)
    # The model sometimes renumbers from 1. Fill any gaps positionally.
    if leftovers:
        for beat in batch:
            if beat["index"] in out:
                continue
            if not leftovers:
                break
            out[beat["index"]] = leftovers.pop(0)
    return out


async def _run_batch(
    batch: list[dict],
    bible: str,
    ctx: dict,
    previous_tail: str,
    seconds: int,
    semaphore: asyncio.Semaphore,
) -> dict[int, dict]:
    prompt = build_batch_prompt(batch, bible, ctx, previous_tail, seconds)
    async with semaphore:
        for attempt in range(SCENE_BATCH_ATTEMPTS):
            try:
                raw = await asyncio.to_thread(
                    chat_json, PROMPT_SYSTEM, prompt, temperature=0.8, max_tokens=8000
                )
            except Exception:
                if attempt:
                    return {}
                continue
            result = _normalize_batch(raw, batch)
            if result:
                return result
    return {}


# --- Thumbnail --------------------------------------------------------------


def generate_thumbnail_prompt(ctx: dict, title: str, bible: str) -> str:
    raw = chat_json(
        THUMBNAIL_SYSTEM,
        (
            f"Video title: {title}\n"
            f"Topic: {ctx.get('topic', '')}\n"
            f"Visual system:\n{bible}\n\n"
            "Return JSON:\n"
            '{ "thumbnail_prompt": "one image-generation prompt in English for a 16:9 '
            "YouTube thumbnail: a single bold subject, a readable emotion, extreme "
            "contrast, shallow depth of field, space on one side for a 2-4 word text "
            'overlay, and the suggested overlay words stated at the end." }\n\n'
            "It must be legible as a 200-pixel-wide rectangle. One subject, not a scene."
        ),
        temperature=0.85,
        max_tokens=900,
    )
    return str(raw.get("thumbnail_prompt", "")).strip() if isinstance(raw, dict) else ""


# --- Orchestration ----------------------------------------------------------


def scene_seconds_for(ctx: dict) -> int:
    raw = ctx.get("scene_seconds") or SCENE_SECONDS
    return max(1, int(raw))


async def build_scenes(doc: dict) -> dict:
    """Full scene pipeline for a project document. Returns the new fields."""
    ctx = doc.get("context", {})
    script = doc.get("script", "")
    title = doc.get("selected_title") or ctx.get("topic", "")
    wpm = int(ctx.get("wpm") or DEFAULT_WPM)
    seconds = scene_seconds_for(ctx)

    beats = segment_beats(script, words_per_scene(wpm, seconds))
    if not beats:
        return {
            "scenes": [],
            "thumbnail_prompt": "",
            "style_bible": "",
            "negative_prompt": "",
            "dropped_scenes": 0,
        }

    # A runaway script is capped, but the creator is told how much was left off
    # rather than quietly handed a package that does not cover the script.
    dropped = max(0, len(beats) - MAX_SCENES)
    beats = beats[:MAX_SCENES]
    for i, beat in enumerate(beats, start=1):
        beat["index"] = i
        beat["shot"] = shot_for(i)

    bible, negative = await asyncio.to_thread(generate_style_bible, ctx, title, script)

    batches = [beats[i : i + SCENE_BATCH_SIZE] for i in range(0, len(beats), SCENE_BATCH_SIZE)]
    semaphore = asyncio.Semaphore(SCENE_BATCH_CONCURRENCY)

    tasks = [
        _run_batch(
            batch,
            bible,
            ctx,
            # Tail of the previous batch, for visual continuity across the seam.
            batches[i - 1][-1]["text"] if i else "",
            seconds,
            semaphore,
        )
        for i, batch in enumerate(batches)
    ]
    thumbnail_task = asyncio.to_thread(generate_thumbnail_prompt, ctx, title, bible)
    *batch_results, thumbnail = await asyncio.gather(
        *tasks, thumbnail_task, return_exceptions=True
    )

    prompts: dict[int, dict] = {}
    for result in batch_results:
        if isinstance(result, dict):
            prompts.update(result)

    # An image is held for exactly as long as its own narration takes, and the
    # start times accumulate. The cadence chosen in the context is the *target*
    # that decides how finely the script is cut, not a fixed slot: the audio is
    # one continuous track, so if the durations did not add up to the narration
    # the pictures would drift further behind with every image.
    scenes = []
    clock = 0.0
    for beat in beats:
        prompt = prompts.get(beat["index"], {})
        est = estimate_seconds(beat["text"], wpm)
        scenes.append(
            {
                "index": beat["index"],
                "start_sec": round(clock, 1),
                "duration_sec": est,
                "narration": beat["text"],
                "context": beat["context"],
                "shot": beat["shot"],
                "est_sec": est,
                "target_sec": seconds,
                "image_prompt": prompt.get("image_prompt", ""),
                "focus": prompt.get("focus", ""),
                "status": "ok" if prompt.get("image_prompt") else "failed",
            }
        )
        clock += est

    return {
        "scenes": scenes,
        "thumbnail_prompt": thumbnail if isinstance(thumbnail, str) else "",
        "style_bible": bible,
        "negative_prompt": negative,
        "dropped_scenes": dropped,
    }


async def retry_failed_scenes(doc: dict) -> list[dict]:
    """Regenerate prompts only for scenes marked failed. Returns all scenes."""
    ctx = doc.get("context", {})
    scenes = doc.get("scenes", [])
    bible = doc.get("style_bible", "")
    seconds = scene_seconds_for(ctx)
    failed = [s for s in scenes if s.get("status") != "ok"]
    if not failed:
        return scenes

    by_index = {s["index"]: s for s in scenes}
    beats = [
        {
            "index": s["index"],
            "text": s.get("narration", ""),
            "context": s.get("context", s.get("narration", "")),
            "shot": s.get("shot") or shot_for(s["index"]),
        }
        for s in failed
    ]
    batches = [beats[i : i + SCENE_BATCH_SIZE] for i in range(0, len(beats), SCENE_BATCH_SIZE)]
    semaphore = asyncio.Semaphore(SCENE_BATCH_CONCURRENCY)
    results = await asyncio.gather(
        *(_run_batch(b, bible, ctx, "", seconds, semaphore) for b in batches),
        return_exceptions=True,
    )
    for result in results:
        if not isinstance(result, dict):
            continue
        for index, prompt in result.items():
            scene = by_index.get(index)
            if scene:
                scene.update(prompt)
                scene["status"] = "ok"
    return scenes
