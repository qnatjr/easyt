"""Request and response schemas."""

from pydantic import BaseModel, Field, field_validator

from app.config import (
    DEFAULT_CATEGORY,
    DEFAULT_LANGUAGE,
    DEFAULT_WPM,
    MAX_MINUTES,
    MAX_SCENE_SECONDS,
    MAX_WPM,
    MIN_MINUTES,
    MIN_SCENE_SECONDS,
    MIN_WPM,
    SCENE_SECONDS,
)


class ProjectCreate(BaseModel):
    topic: str = Field(min_length=1, max_length=2000)
    category: str = DEFAULT_CATEGORY
    language: str = DEFAULT_LANGUAGE
    reference: str = ""
    # A niche from the bank, whose profile is snapshotted into the context.
    niche_id: str = ""
    minutes: float = Field(default=5.0, ge=MIN_MINUTES, le=MAX_MINUTES)
    max_words: int | None = Field(default=None, ge=20, le=20000)
    wpm: int = Field(default=DEFAULT_WPM, ge=MIN_WPM, le=MAX_WPM)
    # How long one still image stays on screen.
    scene_seconds: int = Field(
        default=SCENE_SECONDS, ge=MIN_SCENE_SECONDS, le=MAX_SCENE_SECONDS
    )

    @field_validator("topic")
    @classmethod
    def topic_is_not_only_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("topic cannot be blank")
        return value


class ProjectPatch(BaseModel):
    """Manual edits from the wizard: chosen title, hand-tweaked script, context."""

    topic: str | None = None
    category: str | None = None
    language: str | None = None
    reference: str | None = None
    # "" drops the niche; a non-empty id adopts that one instead.
    niche_id: str | None = None
    minutes: float | None = Field(default=None, ge=MIN_MINUTES, le=MAX_MINUTES)
    max_words: int | None = Field(default=None, ge=20, le=20000)
    wpm: int | None = Field(default=None, ge=MIN_WPM, le=MAX_WPM)
    scene_seconds: int | None = Field(
        default=None, ge=MIN_SCENE_SECONDS, le=MAX_SCENE_SECONDS
    )
    selected_title: str | None = None
    script: str | None = Field(default=None, max_length=400_000)


class ScriptRequest(BaseModel):
    """Optional per-run overrides; unset fields fall back to the project."""

    minutes: float | None = Field(default=None, ge=MIN_MINUTES, le=MAX_MINUTES)
    max_words: int | None = Field(default=None, ge=20, le=20000)
    wpm: int | None = Field(default=None, ge=MIN_WPM, le=MAX_WPM)
    notes: str = ""


class VoiceoverRequest(BaseModel):
    voice_id: str


class VoicePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    speed: float | None = Field(default=None, ge=0.5, le=2.0)
    stability: float | None = Field(default=None, ge=0, le=100)
    similarity: float | None = Field(default=None, ge=0, le=100)
    style: float | None = Field(default=None, ge=0, le=100)
    speaker_boost: bool | None = None
    category: str | None = None
    description: str | None = None
    preset_id: str | None = None


class PreviewRequest(BaseModel):
    text: str = ""


class NicheCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)

    @field_validator("name")
    @classmethod
    def name_is_not_only_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name cannot be blank")
        return value


class NichePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    notes: str | None = Field(default=None, max_length=20_000)
    # Long by design: this is a whole reference video's narration.
    transcript: str | None = Field(default=None, max_length=400_000)
    youtube_url: str | None = Field(default=None, max_length=2000)
