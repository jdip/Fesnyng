"""Browser entrypoint serving the compiled Fesnyng workspace and control-plane API."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from fesnyng_backend.application import HealthResponse, service_health_response
from fesnyng_backend.control_plane import create_app as create_control_plane_app
from fesnyng_backend.settings import ControlPlaneSessionSettings, ServiceSettings


def frontend_dist_from_environment() -> Path:
    """Load the explicit compiled frontend directory for the deployed browser service."""

    configured = os.environ.get("FESNYNG_FRONTEND_DIST")
    if configured is None:
        raise ValueError("FESNYNG_FRONTEND_DIST must name the compiled frontend directory.")
    directory = Path(configured)
    if not directory.is_absolute():
        raise ValueError("FESNYNG_FRONTEND_DIST must be an absolute path.")
    return _validate_frontend_dist(directory)


def _validate_frontend_dist(directory: Path) -> Path:
    resolved = directory.resolve()
    if not resolved.is_dir() or not (resolved / "index.html").is_file():
        raise ValueError("FESNYNG_FRONTEND_DIST must contain a compiled index.html.")
    return resolved


def _static_file(directory: Path, path: str) -> Path | None:
    candidate = (directory / path).resolve()
    try:
        candidate.relative_to(directory)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def create_app(
    settings: ServiceSettings | None = None,
    session_settings: ControlPlaneSessionSettings | None = None,
    frontend_dist: Path | None = None,
    deployed_revision: str | None = None,
) -> FastAPI:
    """Create the public browser surface without changing the direct API entrypoint."""

    directory = (
        _validate_frontend_dist(frontend_dist)
        if frontend_dist
        else frontend_dist_from_environment()
    )
    control_plane = create_control_plane_app(settings, session_settings, deployed_revision)
    app = FastAPI(title="Fesnyng web")
    app.state.control_plane = control_plane

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        del application
        async with control_plane.router.lifespan_context(control_plane):
            yield

    app.router.lifespan_context = lifespan
    app.mount("/api", control_plane)

    @app.get("/health", response_model=HealthResponse, response_model_exclude_none=True)
    def health() -> HealthResponse:
        return service_health_response(control_plane)

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def workspace(path: str) -> FileResponse:
        if path == "api" or path.startswith("api/"):
            raise HTTPException(status_code=404)
        asset = _static_file(directory, path)
        if asset is not None:
            return FileResponse(asset)
        if path.startswith(("assets/", "static/")) or Path(path).suffix:
            raise HTTPException(status_code=404)
        return FileResponse(directory / "index.html")

    return app
