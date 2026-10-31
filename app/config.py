"""Paths, environment and tunable constants for the whole app."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# Overridable so the test suite (and anyone who wants storage on another disk)
# can relocate everything the app writes.
DATA_DIR = Path(os.environ.get("YTC_DATA_DIR") or (BASE_DIR / "data"))
PROJECTS_DIR = DATA_DIR / "projects"
VOICES_DIR = DATA_DIR / "voices"
SAMPLES_DIR = VOICES_DIR / "samples"
VOICES_PATH = VOICES_DIR / "voices.json"
NICHES_DIR = DATA_DIR / "niches"

STYLE_PRESETS_PATH = BASE_DIR / "style_presets.json"
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

# --- MIMO -------------------------------------------------------------------

MIMO_API_KEY = os.environ.get("MIMO_API_KEY", "")
MIMO_BASE_URL = "https://api.xiaomimimo.com/v1"
TEXT_MODEL = "mimo-v2.6-flash"
CLONE_MODEL = "mimo-v2.5-tts-voiceclone"
# Anything carrying a screenshot or a clip goes through VISION_MODEL. Today it
# is the same model as TEXT_MODEL: `mimo-v2.6-flash` takes text, images and mp4
# (frames and audio track in one call), which is why there is no ASR model here
# and nothing converts video to mp3. It stays a separate constant so the two
# routes can diverge again — `mimo-v2.5-pro`, for instance, refused image input.
VISION_MODEL = "mimo-v2.6-flash"

# --- Script budgeting -------------------------------------------------------

DEFAULT_WPM = 150
MIN_WPM = 80
MAX_WPM = 220
MIN_MINUTES = 0.5
MAX_MINUTES = 60.0
# A script may overrun its target by this factor before the tighten pass runs.
DEFAULT_MAX_WORDS_FACTOR = 1.10

# --- Scenes -----------------------------------------------------------------

# One scene is one still image held on screen. Cutting every few seconds is
# what keeps a faceless montage from feeling static, so this is short and
# adjustable per project rather than fixed.
SCENE_SECONDS = 3
MIN_SCENE_SECONDS = 2
MAX_SCENE_SECONDS = 10

# Image prompts are shorter than the old video+image pairs, so a batch can
# carry more of them without the model losing the thread.
SCENE_BATCH_SIZE = 16
SCENE_BATCH_CONCURRENCY = 4
SCENE_BATCH_ATTEMPTS = 2

# Deterministic shot rhythm. Assigned in code and handed to the model, so a
# 200-image montage varies instead of being 200 near-identical wide shots.
SHOT_CYCLE = [
    "wide establishing shot",
    "medium shot",
    "close-up",
    "extreme close-up on a telling detail",
    "medium shot from a new angle",
    "over-the-shoulder / point-of-view",
    "high angle looking down",
    "low angle looking up",
]

# Appended to whatever the style bible says to avoid.
BASE_NEGATIVE_PROMPT = (
    "text, letters, words, watermark, logo, signature, caption, subtitles, "
    "extra fingers, deformed hands, extra limbs, distorted face, blurry, "
    "low quality, jpeg artifacts, oversaturated, cluttered composition"
)
# Runaway guardrail only — it must not bite a legitimate project. At the
# shortest allowed cadence a 60-minute video is 1,800 images, and segments run
# only about 70% full because they break on sentence boundaries, so the real
# worst case is roughly 2,600.
MAX_SCENES = 3000

# --- Voice cloning ----------------------------------------------------------

# MIMO caps the base64 sample string at 10 MB and base64 inflates raw bytes by
# ~33%, so raw uploads are capped at 7 MB to stay under the encoded limit.
MAX_SAMPLE_BYTES = 7 * 1024 * 1024
# The sample is re-sent on every synthesis request, so large samples get costly
# fast on a 75-scene project. Warn above this.
SAMPLE_WARN_BYTES = 2 * 1024 * 1024
ALLOWED_SAMPLE_TYPES = {
    "audio/mpeg": ("mp3", "audio/mpeg"),
    "audio/mp3": ("mp3", "audio/mpeg"),
    "audio/wav": ("wav", "audio/wav"),
    "audio/x-wav": ("wav", "audio/wav"),
    "audio/wave": ("wav", "audio/wav"),
}
VOICEOVER_CONCURRENCY = 3
VOICEOVER_RETRIES = 2

# MIMO publishes no per-request text limit, so long input is split defensively
# on sentence boundaries and each chunk synthesized separately.
MAX_CHUNK_CHARS = 1500

PREVIEW_TEXT = (
    "This is a preview of the cloned voice. "
    "Notice the pacing, the tone, and how each sentence lands."
)

# --- Niche bank -------------------------------------------------------------

# The gateway accepts a 50-second clip and rejects a 55-second one, and reports
# the refusal as "Multimodal data is corrupted or cannot be processed" — which
# is untrue and would send anyone debugging it after the wrong problem. So the
# limit is enforced here, well below the wall, and a clip is never sent that
# could trip it.
NICHE_CLIP_SECONDS = 45
# Payload size is not the constraint: a 3.75 MB 30-second clip is accepted while
# a 0.37 MB 120-second one is not. Duration is the only limit that bites.
NICHE_MAX_CLIP_SECONDS = 50
NICHE_CLIP_COUNT = 4
NICHE_ANALYSIS_CONCURRENCY = 4
NICHE_CLIP_ATTEMPTS = 2

# Downloading at 360p or below keeps the token cost near the measured 220/s
# rather than 276/s at 720p, and a progressive stream needs no merging step.
NICHE_MAX_VIDEO_HEIGHT = 360
MAX_VIDEO_BYTES = 200 * 1024 * 1024
MAX_SHOT_BYTES = 8 * 1024 * 1024
MAX_SHOTS = 12

# Quoted from the API's own rejection: "invalid image format, only
# bmp/gif/png/jpeg/webp are supported".
ALLOWED_IMAGE_TYPES = {
    "image/png": ("png", "image/png"),
    "image/jpeg": ("jpg", "image/jpeg"),
    "image/jpg": ("jpg", "image/jpeg"),
    "image/webp": ("webp", "image/webp"),
    "image/gif": ("gif", "image/gif"),
    "image/bmp": ("bmp", "image/bmp"),
}
ALLOWED_VIDEO_TYPES = {
    "video/mp4": ("mp4", "video/mp4"),
    "video/quicktime": ("mp4", "video/mp4"),
}

# --- Content ----------------------------------------------------------------

LANGUAGES = {"id": "Indonesian", "en": "English"}
DEFAULT_LANGUAGE = "id"

CATEGORIES = [
    "educational",
    "documentary",
    "history",
    "science",
    "technology",
    "true crime",
    "motivation",
    "storytelling",
    "top list",
    "finance",
    "health",
    "mystery",
]
DEFAULT_CATEGORY = "educational"


def ensure_dirs() -> None:
    """Create the on-disk layout. Safe to call repeatedly."""
    for path in (DATA_DIR, PROJECTS_DIR, VOICES_DIR, SAMPLES_DIR, NICHES_DIR):
        path.mkdir(parents=True, exist_ok=True)
