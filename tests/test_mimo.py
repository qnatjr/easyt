"""The multimodal path. No network: the OpenAI client is replaced wholesale."""

import base64
import json

import pytest
from fastapi import HTTPException

from app import mimo


class _Message:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Message(content)


class _Completion:
    def __init__(self, content):
        self.choices = [_Choice(content)]


class _Recorder:
    """Stands in for the OpenAI client and remembers what it was asked for."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.chat = self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _Completion(self.replies.pop(0))


@pytest.fixture
def recorder(monkeypatch):
    def install(*replies):
        rec = _Recorder(replies)
        monkeypatch.setattr(mimo, "get_client", lambda: rec)
        return rec

    return install


def test_image_part_carries_a_data_uri_of_the_bytes():
    part = mimo.image_part(b"\x89PNG-ish", "image/png")

    assert part["type"] == "image_url"
    url = part["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == b"\x89PNG-ish"


def test_video_part_carries_a_data_uri_of_the_bytes():
    part = mimo.video_part(b"\x00\x00\x00\x18ftypmp42", "video/mp4")

    assert part["type"] == "video_url"
    url = part["video_url"]["url"]
    assert url.startswith("data:video/mp4;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == b"\x00\x00\x00\x18ftypmp42"


def test_multimodal_calls_go_to_the_vision_model(recorder):
    # A text-only model answers an image part with "404 No endpoints found
    # that support image input", so multimodal calls must use VISION_MODEL even
    # while it happens to equal TEXT_MODEL.
    from app.config import VISION_MODEL

    rec = recorder(json.dumps({"ok": True}))
    mimo.chat_json_parts("sys", [{"type": "text", "text": "hi"}])

    assert rec.calls[0]["model"] == VISION_MODEL


def test_multimodal_sends_the_parts_as_one_user_message(recorder):
    rec = recorder(json.dumps({"ok": True}))
    parts = [{"type": "text", "text": "describe"}, mimo.image_part(b"x", "image/png")]

    mimo.chat_json_parts("the system prompt", parts)

    messages = rec.calls[0]["messages"]
    assert messages[0] == {"role": "system", "content": "the system prompt"}
    assert messages[1]["role"] == "user"
    assert messages[1]["content"] == parts


def test_multimodal_parses_the_json_reply(recorder):
    recorder(json.dumps({"tone": "wry"}))
    assert mimo.chat_json_parts("sys", [{"type": "text", "text": "x"}]) == {"tone": "wry"}


def test_multimodal_strips_a_wrapping_code_fence(recorder):
    recorder('```json\n{"tone": "wry"}\n```')
    assert mimo.chat_json_parts("sys", [{"type": "text", "text": "x"}]) == {"tone": "wry"}


def test_multimodal_asks_the_model_to_repair_invalid_json(recorder):
    rec = recorder("not json at all", json.dumps({"tone": "wry"}))

    assert mimo.chat_json_parts("sys", [{"type": "text", "text": "x"}]) == {"tone": "wry"}
    assert len(rec.calls) == 2


def test_multimodal_gives_up_with_a_502_after_two_bad_replies(recorder):
    recorder("not json", "still not json")

    with pytest.raises(HTTPException) as exc:
        mimo.chat_json_parts("sys", [{"type": "text", "text": "x"}])
    assert exc.value.status_code == 502


def test_a_reply_that_is_not_an_object_is_a_502(recorder):
    # Every caller does raw.get(...); a bare list would be an AttributeError
    # deep inside a service instead of a clean error here.
    recorder("[1, 2, 3]", "[1, 2, 3]")

    with pytest.raises(HTTPException) as exc:
        mimo.chat_json_parts("sys", [{"type": "text", "text": "x"}])
    assert exc.value.status_code == 502


def test_the_text_only_path_still_uses_the_text_model(recorder):
    from app.config import TEXT_MODEL

    rec = recorder(json.dumps({"ok": True}))
    mimo.chat_json("sys", "user")

    assert rec.calls[0]["model"] == TEXT_MODEL
    assert rec.calls[0]["messages"][1] == {"role": "user", "content": "user"}
