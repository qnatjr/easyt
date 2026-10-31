"""Title ideas with click-through scores.

The scores are the model's judgment of how a title reads, not measured
click-through data. The UI says as much — nothing here touches YouTube.
"""

from app.config import LANGUAGES
from app.mimo import chat_json
from app.services.niche import TITLE_FIELDS, reference_block

TITLE_COUNT = 8
MAX_TITLE_CHARS = 70

SYSTEM = (
    "You are a YouTube packaging strategist for faceless channels. You have studied "
    "thousands of thumbnails and titles and you know precisely what makes a title earn "
    "the click: a curiosity gap, a concrete stake, a number, a contradiction, a named "
    "subject, or an implied consequence. You are blunt about weak titles. "
    "Output ONLY a valid JSON object, no markdown, no commentary."
)


def build_prompt(ctx: dict) -> str:
    language = LANGUAGES.get(ctx.get("language", "id"), "Indonesian")
    reference = (ctx.get("reference") or "").strip()
    ref_line = (
        f"Reference channel or video whose packaging style to echo: {reference}\n"
        if reference
        else ""
    )
    niche = reference_block(ctx.get("niche_profile"), TITLE_FIELDS)
    niche_line = f"{niche}\n" if niche else ""
    return (
        f"Video topic (may be written in any language): {ctx.get('topic', '')}\n"
        f"Category: {ctx.get('category', 'educational')}\n"
        f"Target length: {ctx.get('minutes', 5)} minutes\n"
        f"{ref_line}{niche_line}"
        f"\nWrite the titles in {language}.\n\n"
        f"Produce exactly {TITLE_COUNT} title options as JSON:\n"
        "{\n"
        '  "titles": [\n'
        "    {\n"
        f'      "title": "the title, at most {MAX_TITLE_CHARS} characters",\n'
        '      "ctr_score": <integer 0-100, your honest estimate of relative '
        "click-through strength>,\n"
        '      "hooks": ["the devices used, e.g. curiosity gap, number, '
        'contradiction, stakes, named subject"],\n'
        '      "reason": "one sentence on why this earns the click, and what it risks"\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "Rules:\n"
        "- Spread the scores. Do not give everything 85. If an option is mediocre, "
        "score it mediocre and say why in the reason.\n"
        "- Vary the angle across options: at least one list/number title, one "
        "question, one contrarian claim, one story-driven.\n"
        "- No clickbait that the script cannot pay off.\n"
        "- Sort the array from highest ctr_score to lowest."
    )


def _normalize(raw: dict) -> list[dict]:
    items = raw.get("titles") or []
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    for item in items:
        if isinstance(item, str):
            item = {"title": item}
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).strip()
        if not title:
            continue
        try:
            score = int(round(float(item.get("ctr_score", 0))))
        except (TypeError, ValueError):
            score = 0
        hooks = item.get("hooks") or []
        if isinstance(hooks, str):
            hooks = [hooks]
        out.append(
            {
                "title": title,
                # The model is asked for 0-100 but does not always comply.
                "ctr_score": max(0, min(100, score)),
                "hooks": [str(h).strip() for h in hooks if str(h).strip()][:5],
                "reason": str(item.get("reason", "")).strip(),
                # Recomputed here rather than trusted: models miscount characters.
                "char_count": len(title),
            }
        )
    out.sort(key=lambda t: t["ctr_score"], reverse=True)
    return out


def generate_titles(ctx: dict) -> list[dict]:
    raw = chat_json(SYSTEM, build_prompt(ctx), temperature=0.95, max_tokens=3000)
    return _normalize(raw)
