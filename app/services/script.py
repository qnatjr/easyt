"""Voice-over script generation under a length budget."""

from app.config import DEFAULT_MAX_WORDS_FACTOR, DEFAULT_WPM, LANGUAGES
from app.mimo import chat_json
from app.services.niche import SCRIPT_FIELDS, reference_block

SYSTEM = (
    "You are a senior faceless-channel scriptwriter. You write voice-over that holds "
    "attention: a hook inside the first ten seconds, a promise, tension that keeps "
    "renewing, and a payoff. You write for the ear, not the eye — short sentences, "
    "spoken cadence, no headings, no scene labels, no stage directions. "
    "Output ONLY a valid JSON object, no markdown, no commentary."
)


def word_count(text: str) -> int:
    return len((text or "").split())


def target_words(minutes: float, wpm: int = DEFAULT_WPM) -> int:
    return max(20, int(round(minutes * wpm)))


def resolve_budget(ctx: dict) -> tuple[int, int]:
    """Return (target words, hard maximum words) for a project context.

    A stored `max_words` only counts as a ceiling the creator chose when
    `max_words_custom` says so. Without that flag the derived value would be
    read back as an override, and changing the length or the pace later would
    silently leave the old ceiling in place.
    """
    wpm = int(ctx.get("wpm") or DEFAULT_WPM)
    target = target_words(float(ctx.get("minutes") or 5), wpm)
    max_words = ctx.get("max_words") if ctx.get("max_words_custom") else None
    if not max_words:
        max_words = int(round(target * DEFAULT_MAX_WORDS_FACTOR))
    return target, max(int(max_words), 20)


def max_tokens_for(target: int) -> int:
    """Output budget for a script of `target` words.

    A 60-minute project at 220 wpm is 13,200 words, which is far past any fixed
    8k default — the script would come back cut off mid-sentence.
    """
    return max(4096, min(32768, target * 4))


def build_prompt(ctx: dict, title: str, notes: str = "") -> str:
    language = LANGUAGES.get(ctx.get("language", "id"), "Indonesian")
    target, max_words = resolve_budget(ctx)
    reference = (ctx.get("reference") or "").strip()
    ref_line = f"Reference channel to echo in tone and pacing: {reference}\n" if reference else ""
    notes_line = f"Extra direction from the creator: {notes.strip()}\n" if notes.strip() else ""
    niche = reference_block(ctx.get("niche_profile"), SCRIPT_FIELDS)
    niche_line = f"{niche}\n" if niche else ""
    return (
        f"Video title: {title}\n"
        f"Topic and context: {ctx.get('topic', '')}\n"
        f"Category: {ctx.get('category', 'educational')}\n"
        f"Target length: {ctx.get('minutes', 5)} minutes\n"
        f"Target word count: {target} words. Hard maximum: {max_words} words.\n"
        f"{ref_line}{notes_line}{niche_line}"
        f"\nWrite the script in {language}.\n\n"
        "Return JSON:\n"
        '{ "script": "the full voice-over script as one string" }\n\n'
        "Requirements:\n"
        "- Open with a hook that lands within the first two sentences.\n"
        "- Deliver on the title. Do not bait and switch.\n"
        "- Insert a retention beat near the midpoint that re-opens curiosity.\n"
        "- End with a clear call to action.\n"
        "- Punctuate for narration, not for reading. Commas for short beats, "
        "ellipses for suspense, em dashes for emphasis breaks, periods to land "
        "sentences crisply. Question marks for rhetorical hooks. Go easy on "
        "exclamation marks.\n"
        "- Separate ideas into paragraphs with a blank line between them.\n"
        "- Plain prose only: no scene numbers, no speaker labels, no bracketed "
        "directions, no markdown.\n"
        f"- Stay at or under {max_words} words."
    )


def _extract(raw: dict) -> str:
    script = raw.get("script")
    if isinstance(script, list):
        script = "\n\n".join(str(s) for s in script)
    return str(script or "").strip()


def tighten(script: str, max_words: int, language_code: str) -> str:
    """Ask once for a shorter cut. Returns the original if the retry is worse."""
    language = LANGUAGES.get(language_code, "Indonesian")
    raw = chat_json(
        SYSTEM,
        (
            f"This script is {word_count(script)} words but must be at most "
            f"{max_words} words. Cut it down while keeping the hook, the strongest "
            f"details, the retention beat and the call to action. Keep it in "
            f"{language}. Tighten sentences; do not summarize into bullet points.\n\n"
            f"Script:\n{script}"
        ),
        temperature=0.5,
        max_tokens=max_tokens_for(max_words),
    )
    shorter = _extract(raw)
    if not shorter:
        return script
    return shorter if word_count(shorter) < word_count(script) else script


def generate_script(ctx: dict, title: str, notes: str = "") -> dict:
    target, max_words = resolve_budget(ctx)
    wpm = int(ctx.get("wpm") or DEFAULT_WPM)

    raw = chat_json(
        SYSTEM,
        build_prompt(ctx, title, notes),
        temperature=0.85,
        max_tokens=max_tokens_for(target),
    )
    script = _extract(raw)
    if not script:
        return {
            "script": "",
            "word_count": 0,
            "target_words": target,
            "max_words": max_words,
            "estimated_minutes": 0.0,
            "tightened": False,
            "over_budget": False,
            "under_budget": False,
        }

    tightened = False
    if word_count(script) > max_words:
        script = tighten(script, max_words, ctx.get("language", "id"))
        tightened = True

    words = word_count(script)
    return {
        "script": script,
        "word_count": words,
        "target_words": target,
        "max_words": max_words,
        "estimated_minutes": round(words / wpm, 2) if wpm else 0.0,
        "tightened": tightened,
        # Both directions are surfaced rather than silently accepted: an
        # overrun is the creator's call, and a script well under target usually
        # means the model stopped early on a long request.
        "over_budget": words > max_words,
        "under_budget": words < target * 0.6,
    }
