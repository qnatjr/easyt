"""The analysis job. Media and MIMO are both replaced; nothing leaves the box."""

import asyncio

import pytest

from app.services import niche as niche_service
from app.storage import load_niche, new_niche, niche_video_dir


class FakeMedia:
    """Stands in for app.media, recording what the job asked it to do."""

    def __init__(self, duration=180.0):
        self.duration = duration
        self.cuts = []
        self.downloads = []

    def download_youtube(self, url, dest_dir):
        self.downloads.append(url)
        path = f"{dest_dir}/source.mp4"
        return {
            "path": path,
            "title": "Reference Video",
            "channel": "Reference Channel",
            "duration_sec": self.duration,
        }

    def probe_duration(self, path):
        return self.duration

    def cut_clip(self, src, start, length, dest):
        from pathlib import Path

        self.cuts.append((start, length))
        out = Path(dest)
        out.parent.mkdir(parents=True, exist_ok=True)
        # Each clip gets distinctive bytes so a fake analyser can tell them
        # apart. Clips run concurrently, so a test that keyed off call order
        # would be a race rather than an assertion.
        out.write_bytes(f"clip@{start}".encode())
        return out


def clip_marker(parts) -> str:
    """Recover which clip a set of content parts carries."""
    import base64

    for part in parts:
        if part.get("type") == "video_url":
            uri = part["video_url"]["url"]
            return base64.b64decode(uri.split(",", 1)[1]).decode()
    return ""


@pytest.fixture
def fake_media(monkeypatch):
    media = FakeMedia()
    monkeypatch.setattr(niche_service, "media", media)
    return media


def _run(niche_id):
    asyncio.run(niche_service.run_job(niche_id))


def test_a_pasted_transcript_alone_produces_a_profile(monkeypatch, fake_media):
    monkeypatch.setattr(niche_service, "chat_json", lambda *a, **k: {"tone": "brisk"})

    doc = new_niche("Text only")
    doc["transcript"] = "narration the creator pasted"
    niche_service.save_niche(doc)

    _run(doc["id"])

    saved = load_niche(doc["id"])
    assert saved["analysis"]["status"] == "done"
    assert saved["profile"]["tone"] == "brisk"
    # No video and no link, so the media layer must never have been asked.
    assert fake_media.cuts == []
    assert fake_media.downloads == []


def test_a_youtube_link_is_downloaded_then_cut_and_analysed(monkeypatch, fake_media):
    monkeypatch.setattr(
        niche_service,
        "chat_json_parts",
        lambda *a, **k: {"transcript": "a slice", "tone": "wry"},
    )
    monkeypatch.setattr(niche_service, "chat_json", lambda *a, **k: {"tone": "wry"})

    doc = new_niche("From a link")
    doc["source"]["youtube_url"] = "https://youtu.be/x"
    niche_service.save_niche(doc)

    _run(doc["id"])

    saved = load_niche(doc["id"])
    assert saved["analysis"]["status"] == "done"
    assert saved["source"]["title"] == "Reference Video"
    assert saved["source"]["channel"] == "Reference Channel"
    assert saved["source"]["duration_sec"] == 180.0
    # 180s at 45s a clip is four, and they must not overlap.
    assert fake_media.cuts == [(0.0, 45.0), (45.0, 45.0), (90.0, 45.0), (135.0, 45.0)]
    assert saved["transcript"].count("a slice") == 4


def test_an_already_downloaded_video_is_not_fetched_again(monkeypatch, fake_media):
    monkeypatch.setattr(
        niche_service, "chat_json_parts", lambda *a, **k: {"transcript": "s"}
    )
    monkeypatch.setattr(niche_service, "chat_json", lambda *a, **k: {"tone": "wry"})

    doc = new_niche("Already here")
    doc["source"]["youtube_url"] = "https://youtu.be/x"
    doc["source"]["video_file"] = "source.mp4"
    niche_service.save_niche(doc)
    video = niche_video_dir(doc["id"]) / "source.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"already downloaded")

    _run(doc["id"])

    assert fake_media.downloads == []
    assert fake_media.cuts


def test_one_dead_clip_leaves_the_run_partial_and_names_the_gap(monkeypatch, fake_media):
    attempts = {"n": 0}

    def flaky(system, parts, **k):
        # Keyed off the clip itself, so concurrency cannot make this a race.
        if clip_marker(parts) == "clip@45.0":
            attempts["n"] += 1
            raise RuntimeError("upstream said no")
        return {"transcript": "a slice"}

    monkeypatch.setattr(niche_service, "chat_json_parts", flaky)
    monkeypatch.setattr(niche_service, "chat_json", lambda *a, **k: {"tone": "wry"})

    doc = new_niche("One bad clip")
    doc["source"]["youtube_url"] = "https://youtu.be/x"
    niche_service.save_niche(doc)

    _run(doc["id"])

    saved = load_niche(doc["id"])
    assert saved["analysis"]["status"] == "partial"
    assert saved["analysis"]["failed_ranges"] == ["00:45-01:30"]
    # A hole in the middle is worth saying out loud, but the rest is still
    # worth keeping rather than throwing the whole run away.
    assert saved["profile"]["tone"] == "wry"
    assert saved["transcript"].count("a slice") == 3
    # A dead clip is retried before being given up on.
    assert attempts["n"] == 2


def test_every_clip_failing_is_a_failed_run(monkeypatch, fake_media):
    def always_dead(*a, **k):
        raise RuntimeError("upstream is down")

    monkeypatch.setattr(niche_service, "chat_json_parts", always_dead)
    monkeypatch.setattr(niche_service, "chat_json", lambda *a, **k: {"tone": "wry"})

    doc = new_niche("All bad")
    doc["source"]["youtube_url"] = "https://youtu.be/x"
    niche_service.save_niche(doc)

    _run(doc["id"])

    saved = load_niche(doc["id"])
    assert saved["analysis"]["status"] == "failed"
    assert saved["profile"] == {}


def test_a_failed_download_does_not_destroy_what_the_niche_already_had(
    monkeypatch, fake_media
):
    from fastapi import HTTPException

    def blocked(*a, **k):
        raise HTTPException(status_code=502, detail="download failed: not a bot")

    monkeypatch.setattr(fake_media, "download_youtube", blocked)
    monkeypatch.setattr(niche_service, "chat_json", lambda *a, **k: {"tone": "kept"})

    doc = new_niche("Blocked")
    doc["source"]["youtube_url"] = "https://youtu.be/x"
    doc["notes"] = "notes worth keeping"
    niche_service.save_niche(doc)

    _run(doc["id"])

    saved = load_niche(doc["id"])
    assert saved["analysis"]["status"] == "failed"
    assert "not a bot" in saved["analysis"]["error"]
    assert saved["notes"] == "notes worth keeping"


def test_a_niche_with_nothing_in_it_fails_with_a_reason(monkeypatch, fake_media):
    doc = new_niche("Empty")

    _run(doc["id"])

    saved = load_niche(doc["id"])
    assert saved["analysis"]["status"] == "failed"
    assert saved["analysis"]["error"]


def test_the_job_reports_itself_running_and_then_stops(monkeypatch, fake_media):
    seen = {}

    def peek(*a, **k):
        seen["during"] = niche_service.is_running(nid)
        return {"tone": "wry"}

    monkeypatch.setattr(niche_service, "chat_json", peek)

    doc = new_niche("Watched")
    doc["transcript"] = "some narration"
    niche_service.save_niche(doc)
    nid = doc["id"]

    assert niche_service.is_running(nid) is False
    _run(nid)

    assert seen["during"] is True
    assert niche_service.is_running(nid) is False
