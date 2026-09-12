"""Shared FastAPI surface for service-owned operational contracts."""

from __future__ import annotations

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


def create_service_app(settings: ServiceSettings) -> FastAPI:
    """Build an independently configured service with durable identity."""

    identity = initialize_service_state(settings)
    app = FastAPI(title=f"Fesnyng {settings.service}")

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(
            service=identity.service,
            instance_id=identity.instance_id,
            schema_version=identity.schema_version,
        )

    return app
