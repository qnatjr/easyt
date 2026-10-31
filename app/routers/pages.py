"""HTML routes. All data loading happens client-side against the JSON API."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import (
    CATEGORIES,
    DEFAULT_CATEGORY,
    DEFAULT_LANGUAGE,
    DEFAULT_WPM,
    LANGUAGES,
    MAX_MINUTES,
    MAX_SCENE_SECONDS,
    MIN_MINUTES,
    MIN_SCENE_SECONDS,
    SAMPLE_WARN_BYTES,
    SCENE_SECONDS,
    SHOT_CYCLE,
    TEMPLATES_DIR,
)
from app.storage import load_project

router = APIRouter(tags=["pages"])
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

DEFAULTS = {
    "categories": CATEGORIES,
    "default_category": DEFAULT_CATEGORY,
    "languages": LANGUAGES,
    "default_language": DEFAULT_LANGUAGE,
    "default_wpm": DEFAULT_WPM,
    "min_minutes": MIN_MINUTES,
    "max_minutes": MAX_MINUTES,
    "scene_seconds": SCENE_SECONDS,
    "min_scene_seconds": MIN_SCENE_SECONDS,
    "max_scene_seconds": MAX_SCENE_SECONDS,
    "shot_cycle": SHOT_CYCLE,
    "sample_warn_bytes": SAMPLE_WARN_BYTES,
}


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse(
        request, "dashboard.html", {"nav": "dashboard", "defaults": DEFAULTS}
    )


@router.get("/projects/{project_id}", response_class=HTMLResponse)
def project_page(request: Request, project_id: str):
    doc = load_project(project_id)
    return templates.TemplateResponse(
        request,
        "project.html",
        {
            "nav": "dashboard",
            "defaults": DEFAULTS,
            "project_id": project_id,
            "topic": doc.get("context", {}).get("topic", ""),
        },
    )


@router.get("/voices", response_class=HTMLResponse)
def voices_page(request: Request):
    return templates.TemplateResponse(
        request, "voices.html", {"nav": "voices", "defaults": DEFAULTS}
    )


@router.get("/niches", response_class=HTMLResponse)
def niches_page(request: Request):
    return templates.TemplateResponse(
        request, "niches.html", {"nav": "niches", "defaults": DEFAULTS}
    )


@router.get("/health")
def health():
    return {"status": "ok"}
