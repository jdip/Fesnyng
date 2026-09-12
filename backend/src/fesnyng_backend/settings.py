"""Configuration shared by independently runnable Fesnyng services."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

ServiceName = Literal["control-plane", "agent-host"]


class ServiceSettings(BaseModel):
    """Filesystem and identity inputs for one durable service instance."""

    model_config = ConfigDict(frozen=True)

    service: ServiceName
    database_path: Path
    state_directory: Path
    requested_instance_id: UUID | None = Field(default=None, alias="instance_id")

    @model_validator(mode="after")
    def reject_state_directory_as_database_path(self) -> ServiceSettings:
        if self.database_path == self.state_directory:
            raise ValueError("database path must not equal the state directory")
        return self


class ControlPlaneSessionSettings(BaseModel):
    """Browser-session settings kept separate from durable service identity."""

    model_config = ConfigDict(frozen=True)

    cookie_name: str = "fesnyng_session"
    cookie_secure: bool = True
    allowed_origin: str | None = None
    session_lifetime_seconds: int = Field(default=60 * 60 * 24 * 7, ge=60, le=60 * 60 * 24 * 31)


def settings_from_environment(service: ServiceName) -> ServiceSettings:
    """Read only this service's local configuration from its environment."""

    prefix = f"FESNYNG_{service.replace('-', '_').upper()}"
    state_directory = Path(os.environ.get(f"{prefix}_STATE_DIRECTORY", f"./.fesnyng/{service}"))
    database_path = Path(
        os.environ.get(f"{prefix}_DATABASE_PATH", str(state_directory / "fesnyng.sqlite3"))
    )
    instance_id = os.environ.get(f"{prefix}_INSTANCE_ID")
    return ServiceSettings(
        service=service,
        database_path=database_path,
        state_directory=state_directory,
        instance_id=instance_id,
    )


def control_plane_session_settings_from_environment() -> ControlPlaneSessionSettings:
    """Use secure cookies unless explicit local-development configuration opts out."""

    local_development = os.environ.get("FESNYNG_CONTROL_PLANE_LOCALHOST_DEVELOPMENT") == "true"
    return ControlPlaneSessionSettings(
        cookie_name=os.environ.get("FESNYNG_CONTROL_PLANE_SESSION_COOKIE_NAME", "fesnyng_session"),
        cookie_secure=not local_development,
        allowed_origin=os.environ.get("FESNYNG_CONTROL_PLANE_ALLOWED_ORIGIN"),
    )
