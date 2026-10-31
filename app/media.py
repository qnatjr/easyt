"""The yt-dlp and ffmpeg boundary.

Everything that shells out or reaches YouTube lives here, so the rest of the
app — and the whole test suite — can stay offline by replacing this one module.

Both dependencies are optional at runtime. Importing them lazily is the point:
a machine without them still starts the server and still runs the parts of the
niche bank that need neither, namely pasted transcripts and screenshots.
"""

import re
import subprocess
from pathlib import Path

from fastapi import HTTPException

from app.config import NICHE_MAX_VIDEO_HEIGHT

# "  Duration: 00:02:26.72, start: 0.000000, bitrate: 375 kb/s"
_DURATION = re.compile(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)")


def _import_ytdlp():
    try:
        import yt_dlp

        return yt_dlp
    except ImportError:
        return None


def ffmpeg_exe() -> str | None:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        return None


def require_ffmpeg() -> str:
    exe = ffmpeg_exe()
    if not exe:
        raise HTTPException(
            status_code=503,
            detail=(
                "ffmpeg is not available, so a video cannot be cut into clips. "
                "Install it with: pip install imageio-ffmpeg"
            ),
        )
    return exe


def require_ytdlp():
    module = _import_ytdlp()
    if module is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "yt-dlp is not installed, so YouTube links cannot be downloaded. "
                "Install it with: pip install yt-dlp"
            ),
        )
    return module


def parse_duration(ffmpeg_output: str) -> float:
    """Read a duration out of ffmpeg's banner.

    `imageio-ffmpeg` ships ffmpeg but not ffprobe, so the duration comes from
    the stderr banner ffmpeg prints when it opens a file. A file it cannot read
    prints no Duration line, and 0.0 is the honest answer for that.
    """
    match = _DURATION.search(ffmpeg_output or "")
    if not match:
        return 0.0
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def probe_duration(path: str | Path) -> float:
    """How long a local video is, in seconds. 0.0 if it cannot be read."""
    exe = require_ffmpeg()
    # No output file: ffmpeg prints the banner, complains, and exits non-zero.
    # The banner is all we want, so the exit code is deliberately ignored.
    proc = subprocess.run(
        [exe, "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    return parse_duration(proc.stderr)


def clip_argv(
    exe: str, src: str | Path, start: float, length: float, dest: str | Path
) -> list[str]:
    """The command that cuts one clip.

    `-ss` goes before `-i` so ffmpeg seeks instead of decoding everything up to
    the start point — the difference between instant and a minute on a long
    reference. The clip is re-encoded rather than stream-copied because a copy
    begins at the nearest keyframe before the cut, and the torn frames that
    produces are exactly what the model would be looking at.
    """
    return [
        exe,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        str(start),
        "-i",
        str(src),
        "-t",
        str(length),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "28",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "64k",
        "-movflags",
        "+faststart",
        str(dest),
    ]


def cut_clip(src: str | Path, start: float, length: float, dest: str | Path) -> Path:
    exe = require_ffmpeg()
    proc = subprocess.run(
        clip_argv(exe, src, start, length, dest),
        capture_output=True,
        text=True,
        timeout=600,
    )
    out = Path(dest)
    if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        detail = (proc.stderr or "ffmpeg produced no clip").strip()[:300]
        raise HTTPException(status_code=502, detail=f"could not cut the clip: {detail}")
    return out


def download_youtube(url: str, dest_dir: str | Path) -> dict:
    """Fetch a reference video and report what came back.

    A progressive stream is requested — one file with both tracks already
    muxed. Merging separate video and audio streams would make ffmpeg a hard
    requirement for downloading, and capping the height keeps the later token
    cost near the measured 220/s of 360p rather than 276/s at 720p.
    """
    module = require_ytdlp()
    options = {
        "format": (
            f"best[ext=mp4][height<={NICHE_MAX_VIDEO_HEIGHT}]"
            f"/best[height<={NICHE_MAX_VIDEO_HEIGHT}]"
            f"/best[ext=mp4]/best"
        ),
        "outtmpl": str(Path(dest_dir) / "source.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }
    try:
        with module.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as e:  # yt-dlp raises its own hierarchy plus anything urllib does
        raise HTTPException(status_code=502, detail=f"download failed: {str(e)[:300]}")

    downloads = info.get("requested_downloads") or []
    path = downloads[0].get("filepath") if downloads else info.get("_filename")
    if not path:
        raise HTTPException(status_code=502, detail="the download produced no file")

    return {
        "path": path,
        "title": info.get("title") or "",
        "channel": info.get("channel") or info.get("uploader") or "",
        "duration_sec": float(info.get("duration") or 0),
    }
