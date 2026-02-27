"""FastAPI application factory for the ottonate dashboard."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ottonate.config import OttonateConfig
from ottonate.github import GitHubClient
from ottonate.metrics import MetricsStore

from .api import router as api_router
from .views import router as views_router

_HERE = Path(__file__).parent


def create_app(config: OttonateConfig | None = None) -> FastAPI:
    if config is None:
        config = OttonateConfig()

    metrics = MetricsStore(config.resolved_db_path())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await metrics.init_db()
        yield

    app = FastAPI(title="Ottonate Dashboard", lifespan=lifespan)

    app.state.config = config
    app.state.github = GitHubClient()
    app.state.metrics = metrics
    app.state.templates = Jinja2Templates(directory=str(_HERE / "templates"))

    app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")
    app.include_router(api_router, prefix="/api")
    app.include_router(views_router)

    return app
