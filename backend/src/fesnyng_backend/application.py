"""Shared FastAPI surface for service-owned operational contracts."""

from __future__ import annotations

import os
import re
from typing import Literal
from uuid import UUID

from fastapi import FastAPI
from pydantic import BaseModel

from fesnyng_backend.settings import ServiceName, ServiceSettings
from fesnyng_backend.storage import initialize_service_state


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: ServiceName
    instance_id: UUID
    schema_version: int
    revision: str | None = None


def deployed_revision_from_environment() -> str | None:
    """Return the release revision that a deployment health check can verify."""

    revision = os.environ.get("FESNYNG_DEPLOYED_REVISION")
    if revision is None:
        return None
    return validate_deployed_revision(revision)


def validate_deployed_revision(revision: str) -> str:
    """Reject a value that cannot identify one exact deployed Git revision."""

    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("FESNYNG_DEPLOYED_REVISION must be a lowercase 40-character Git SHA.")
    return revision


def service_health_response(application: FastAPI) -> HealthResponse:
    """Build the public health response from a service created by this module."""

    identity = application.state.service_identity
    return HealthResponse(
        service=identity.service,
        instance_id=identity.instance_id,
        schema_version=identity.schema_version,
        revision=application.state.deployed_revision,
    )


def create_service_app(settings: ServiceSettings, deployed_revision: str | None = None) -> FastAPI:
    """Build an independently configured service with durable identity."""

    identity = initialize_service_state(settings)
    revision = (
        validate_deployed_revision(deployed_revision)
        if deployed_revision is not None
        else deployed_revision_from_environment()
    )
    app = FastAPI(title=f"Fesnyng {settings.service}")
    app.state.service_identity = identity
    app.state.deployed_revision = revision

    @app.get("/health", response_model=HealthResponse, response_model_exclude_none=True)
    def health() -> HealthResponse:
        return service_health_response(app)

    return app
