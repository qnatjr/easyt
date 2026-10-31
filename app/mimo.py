"""Thin wrapper around the MIMO API.

MIMO speaks the OpenAI chat-completions protocol, so the official SDK is used
with a different base URL. Everything that talks to MIMO goes through here so
the services layer can be tested with a single mock.
"""

import base64
import json
import re

from fastapi import HTTPException
from openai import OpenAI, OpenAIError

from app.config import (
    CLONE_MODEL,
    MIMO_API_KEY,
    MIMO_BASE_URL,
    TEXT_MODEL,
    VISION_MODEL,
)

_client: OpenAI | None = None

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def get_client() -> OpenAI:
    """Lazily build the client so importing the app never needs a key."""
    global _client
    if _client is None:
        if not MIMO_API_KEY:
            raise HTTPException(
                status_code=503,
                detail="MIMO_API_KEY is not set. Add it to .env and restart the server.",
            )
        _client = OpenAI(api_key=MIMO_API_KEY, base_url=MIMO_BASE_URL)
    return _client


def short_error(e: Exception) -> str:
    """Upstream errors can embed the entire base64 sample; strip it."""
    msg = str(e)
    i = msg.find("base64,")
    if i != -1:
        msg = msg[: i + 7] + "…<sample>…"
    return msg[:300]


def _loads(raw: str) -> dict:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # The model occasionally wraps its JSON in a code fence.
        return json.loads(_FENCE.sub("", raw.strip()).strip())


def _ask_for_json(
    messages: list[dict], model: str, max_tokens: int, temperature: float
) -> dict:
    """Send `messages`, expect one JSON object back, and insist on getting one.

    Two recovery stages: strip a wrapping code fence, then ask the model once to
    repair its own output. A third failure is a 502.

    Shared by the text-only and the multimodal entry points so a fix to the
    recovery behaviour cannot apply to one and miss the other.
    """
    client = get_client()

    def send(msgs: list[dict], temp: float):
        try:
            completion = client.chat.completions.create(
                model=model,
                messages=msgs,
                response_format={"type": "json_object"},
                temperature=temp,
                max_tokens=max_tokens,
            )
        except OpenAIError as e:
            raise HTTPException(status_code=502, detail=f"MIMO failed: {short_error(e)}")
        return completion.choices[0].message.content or ""

    raw = send(messages, temperature)
    try:
        parsed = _loads(raw)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed

    repaired_raw = send(
        messages
        + [
            {"role": "assistant", "content": raw[:6000]},
            {
                "role": "user",
                "content": (
                    "That response was not a valid JSON object. Return the same content "
                    "as a single valid JSON object, not an array and not a bare value. "
                    "Output only the JSON, nothing else."
                ),
            },
        ],
        0.2,
    )
    try:
        repaired = _loads(repaired_raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=502, detail="MIMO returned invalid JSON twice")

    # Every caller does raw.get(...); a list or a bare value would be an
    # AttributeError deep in a service rather than a clean 502 here.
    if not isinstance(repaired, dict):
        raise HTTPException(
            status_code=502, detail="MIMO returned JSON that was not an object"
        )
    return repaired


def chat_json(
    system: str,
    user: str,
    *,
    temperature: float = 0.8,
    max_tokens: int = 8192,
) -> dict:
    """Ask MIMO for a JSON object and return it parsed."""
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    return _ask_for_json(messages, TEXT_MODEL, max_tokens, temperature)


def image_part(raw: bytes, mime: str) -> dict:
    """One screenshot, as the content part the API expects."""
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime};base64,{base64.b64encode(raw).decode()}"},
    }


def video_part(raw: bytes, mime: str = "video/mp4") -> dict:
    """One clip. The model reads its frames and its audio track both."""
    return {
        "type": "video_url",
        "video_url": {"url": f"data:{mime};base64,{base64.b64encode(raw).decode()}"},
    }


def chat_json_parts(
    system: str,
    parts: list[dict],
    *,
    temperature: float = 0.4,
    max_tokens: int = 8192,
) -> dict:
    """Ask the vision model for a JSON object about images and/or video.

    Routed through `VISION_MODEL` rather than `TEXT_MODEL` so the two can point
    at different models: a text-only model answers any image part with
    "404 No endpoints found that support image input".
    """
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": parts},
    ]
    return _ask_for_json(messages, VISION_MODEL, max_tokens, temperature)


def _extract_audio_bytes(message) -> bytes:
    audio = getattr(message, "audio", None)
    data = None
    if isinstance(audio, dict):
        data = audio.get("data")
    elif audio is not None:
        data = getattr(audio, "data", None)
    if not data:
        raise HTTPException(status_code=502, detail="MIMO returned no audio")
    return base64.b64decode(data)


def synth_clone(text: str, sample_b64: str, mime: str, style_prompt: str) -> bytes:
    """Synthesize `text` in the cloned voice described by the base64 sample."""
    client = get_client()
    try:
        completion = client.chat.completions.create(
            model=CLONE_MODEL,
            messages=[
                {"role": "user", "content": style_prompt or ""},
                {"role": "assistant", "content": text},
            ],
            audio={"format": "wav", "voice": f"data:{mime};base64,{sample_b64}"},
        )
    except OpenAIError as e:
        raise HTTPException(status_code=502, detail=f"MIMO synth failed: {short_error(e)}")
    return _extract_audio_bytes(completion.choices[0].message)
