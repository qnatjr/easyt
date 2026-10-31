"""YouTube Creation Dashboard — idea to titles to script to scenes to voice-over."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import STATIC_DIR, ensure_dirs
from app.routers import niches, pages, projects, voices


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_dirs()
    yield


app = FastAPI(
    title="YouTube Creation Dashboard",
    description="Faceless-video pipeline: context, titles, script, scenes, voice-over.",
    version="1.0.0",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(pages.router)
app.include_router(projects.router)
app.include_router(voices.router)
app.include_router(voices.presets_router)
app.include_router(niches.router)
