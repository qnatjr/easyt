"""Derived export artefacts: script.txt and prompts.csv."""

import csv
import io
from pathlib import Path

from app.audio import timecode
from app.storage import project_dir

CSV_COLUMNS = [
    "scene",
    "start",
    "end",
    "duration_sec",
    "target_sec",
    "shot",
    "focus",
    "narration",
    "image_prompt",
    "negative_prompt",
]


def script_text(doc: dict) -> str:
    """The script exactly as it will be narrated — no header, no metadata.

    Kept pure so the file can be fed straight back into a TTS tool.
    """
    return (doc.get("script") or "").strip() + "\n"


def prompts_csv(doc: dict) -> str:
    """One row per scene, then a final row holding the thumbnail prompt."""
    negative = (doc.get("negative_prompt") or "").strip()
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for scene in doc.get("scenes", []):
        start = scene.get("start_sec", 0)
        # How long the image is up: its own narration, measured once the
        # voice-over has run. The times accumulate, so the shot list stays in
        # step with the single continuous audio track.
        duration = scene.get("duration_sec", 0)
        writer.writerow(
            [
                scene.get("index", ""),
                timecode(start),
                timecode(start + duration),
                duration,
                # The cadence the creator asked for, for comparison.
                scene.get("target_sec", ""),
                scene.get("shot", ""),
                scene.get("focus", ""),
                scene.get("narration", ""),
                scene.get("image_prompt", ""),
                negative,
            ]
        )
    thumbnail = (doc.get("thumbnail_prompt") or "").strip()
    if thumbnail:
        writer.writerow(
            ["thumbnail", "", "", "", "", "", "", "", thumbnail, negative]
        )
    return buf.getvalue()


def write_exports(doc: dict) -> dict[str, Path]:
    """Refresh the on-disk exports for a project. Returns the paths written.

    An export whose source no longer exists is deleted rather than left behind:
    regenerating a script clears the scenes, and a stale prompts.csv describing
    the previous breakdown would otherwise survive in the directory and in the
    bundle download.
    """
    base = project_dir(doc["id"])
    base.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    script_path = base / "script.txt"
    if doc.get("script"):
        script_path.write_text(script_text(doc), encoding="utf-8")
        written["script"] = script_path
    else:
        script_path.unlink(missing_ok=True)

    prompts_path = base / "prompts.csv"
    if doc.get("scenes") or doc.get("thumbnail_prompt"):
        # utf-8-sig so Excel opens the file with the right encoding.
        prompts_path.write_text(prompts_csv(doc), encoding="utf-8-sig")
        written["prompts"] = prompts_path
    else:
        prompts_path.unlink(missing_ok=True)

    return written
