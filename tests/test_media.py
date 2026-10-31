"""The yt-dlp and ffmpeg boundary. Nothing here runs either binary."""

import pytest
from fastapi import HTTPException

from app import media

# Real ffmpeg banner output, trimmed to the lines that matter.
FFMPEG_STDERR = """ffmpeg version 7.1 Copyright (c) 2000-2024 the FFmpeg developers
Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'clip.mp4':
  Metadata:
    major_brand     : isom
  Duration: 00:02:26.72, start: 0.000000, bitrate: 375 kb/s
  Stream #0:0[0x1](und): Video: h264 (avc1 / 0x31637661), yuv420p, 640x360
"""


def test_duration_is_read_from_the_ffmpeg_banner():
    assert media.parse_duration(FFMPEG_STDERR) == pytest.approx(146.72)


def test_an_hour_long_duration_is_read_correctly():
    assert media.parse_duration("  Duration: 01:02:03.50, start: 0.0") == pytest.approx(3723.5)


def test_output_with_no_duration_reads_as_zero():
    assert media.parse_duration("") == 0.0
    assert media.parse_duration("ffmpeg version 7.1\nInvalid data found") == 0.0
    assert media.parse_duration("Duration: N/A, start: 0.000000") == 0.0


def test_the_cut_command_bounds_both_ends_of_the_clip():
    argv = media.clip_argv("/bin/ffmpeg", "src.mp4", 90.0, 45.0, "out.mp4")

    assert argv[0] == "/bin/ffmpeg"
    # -ss must precede -i, or ffmpeg decodes everything up to the start point.
    assert argv.index("-ss") < argv.index("-i")
    assert argv[argv.index("-ss") + 1] == "90.0"
    assert argv[argv.index("-t") + 1] == "45.0"
    assert argv[-1] == "out.mp4"


def test_the_cut_command_re_encodes_rather_than_stream_copying():
    # A stream copy starts at the nearest keyframe before the cut, so the first
    # frames of the clip can be torn. The model is going to look at them.
    argv = media.clip_argv("/bin/ffmpeg", "src.mp4", 0.0, 45.0, "out.mp4")
    assert "-c" not in argv or "copy" not in argv
    assert "libx264" in argv


def test_a_missing_ffmpeg_is_a_503_naming_the_package(monkeypatch):
    monkeypatch.setattr(media, "ffmpeg_exe", lambda: None)

    with pytest.raises(HTTPException) as exc:
        media.require_ffmpeg()
    assert exc.value.status_code == 503
    assert "imageio-ffmpeg" in exc.value.detail


def test_a_missing_yt_dlp_is_a_503_naming_the_package(monkeypatch):
    monkeypatch.setattr(media, "_import_ytdlp", lambda: None)

    with pytest.raises(HTTPException) as exc:
        media.require_ytdlp()
    assert exc.value.status_code == 503
    assert "yt-dlp" in exc.value.detail


def test_a_blocked_download_surfaces_the_real_message(monkeypatch):
    class Boom:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, *a, **k):
            raise RuntimeError("Sign in to confirm you're not a bot")

    monkeypatch.setattr(media, "_import_ytdlp", lambda: type("M", (), {"YoutubeDL": Boom}))

    with pytest.raises(HTTPException) as exc:
        media.download_youtube("https://youtu.be/x", "/tmp")
    assert exc.value.status_code == 502
    assert "not a bot" in exc.value.detail


def test_the_download_asks_for_a_progressive_stream_within_the_height_cap(monkeypatch):
    from app.config import NICHE_MAX_VIDEO_HEIGHT

    captured = {}

    class Fake:
        def __init__(self, opts):
            captured["opts"] = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=True):
            return {
                "title": "A Video",
                "channel": "A Channel",
                "duration": 612,
                "requested_downloads": [{"filepath": "/tmp/out.mp4"}],
            }

    monkeypatch.setattr(media, "_import_ytdlp", lambda: type("M", (), {"YoutubeDL": Fake}))

    info = media.download_youtube("https://youtu.be/x", "/tmp")

    assert info["title"] == "A Video"
    assert info["channel"] == "A Channel"
    assert info["duration_sec"] == 612
    assert info["path"] == "/tmp/out.mp4"
    # A progressive stream needs no merge step, which is what keeps ffmpeg
    # optional for the download half of the feature.
    assert str(NICHE_MAX_VIDEO_HEIGHT) in captured["opts"]["format"]
