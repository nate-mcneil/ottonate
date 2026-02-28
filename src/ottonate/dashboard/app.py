"""FastAPI application factory for the ottonate dashboard."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ottonate.config import OttonateConfig
from ottonate.github import GitHubClient

from .api import router as api_router
from .views import router as views_router

_HERE = Path(__file__).parent


def create_app(config: OttonateConfig | None = None) -> FastAPI:
    if config is None:
        config = OttonateConfig()

    app = FastAPI(title="Ottonate Dashboard")

    app.state.config = config
    app.state.github = GitHubClient()
    app.state.templates = Jinja2Templates(directory=str(_HERE / "templates"))

    app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")
    app.include_router(api_router, prefix="/api")
    app.include_router(views_router)

    return app
