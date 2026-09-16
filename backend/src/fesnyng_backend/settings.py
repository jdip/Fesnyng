"""Configuration shared by independently runnable Fesnyng services."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

ServiceName = Literal["control-plane", "agent-host"]


class DockerCapabilitySettings(BaseModel):
    """Explicit administrator grant of one organization-dedicated Docker engine."""

    model_config = ConfigDict(frozen=True)

    organization_id: UUID
    engine_id: str = Field(min_length=1)
    socket_path: Path
    employee_ids: tuple[UUID, ...] = ()

    @model_validator(mode="after")
    def require_safe_socket_path(self) -> DockerCapabilitySettings:
        if not self.socket_path.is_absolute():
            raise ValueError("Docker capability socket path must be absolute")
        if not self.engine_id.strip():
            raise ValueError("Docker capability engine id must not be blank")
        return self


class ServiceSettings(BaseModel):
    """Filesystem and identity inputs for one durable service instance."""

    model_config = ConfigDict(frozen=True)

    service: ServiceName
    database_path: Path
    state_directory: Path
    workspace_root: Path | None = None
    requested_instance_id: UUID | None = Field(default=None, alias="instance_id")
    docker_capability: DockerCapabilitySettings | None = None

    @model_validator(mode="after")
    def reject_state_directory_as_database_path(self) -> ServiceSettings:
        if self.database_path == self.state_directory:
            raise ValueError("database path must not equal the state directory")
        if self.workspace_root is not None and not self.workspace_root.is_absolute():
            raise ValueError("workspace root must be an absolute path")
        if self.docker_capability is not None and self.service != "agent-host":
            raise ValueError("Only an agent host may configure Docker capability")
        return self

    @property
    def agent_workspace_root(self) -> Path:
        """Return the durable host-owned root for newly prepared workspaces."""
        if self.service != "agent-host":
            raise ValueError("Only an agent host has a workspace root")
        return (self.workspace_root or (self.state_directory.resolve() / "workspaces")).resolve()


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
    workspace_root = os.environ.get(f"{prefix}_WORKSPACE_ROOT")
    docker_capability = os.environ.get(f"{prefix}_DOCKER_CAPABILITY")
    return ServiceSettings(
        service=service,
        database_path=database_path,
        state_directory=state_directory,
        instance_id=instance_id,
        workspace_root=Path(workspace_root) if workspace_root else None,
        docker_capability=(
            DockerCapabilitySettings.model_validate_json(docker_capability)
            if docker_capability
            else None
        ),
    )


def control_plane_session_settings_from_environment() -> ControlPlaneSessionSettings:
    """Use secure cookies unless explicit local-development configuration opts out."""

    local_development = os.environ.get("FESNYNG_CONTROL_PLANE_LOCALHOST_DEVELOPMENT") == "true"
    return ControlPlaneSessionSettings(
        cookie_name=os.environ.get("FESNYNG_CONTROL_PLANE_SESSION_COOKIE_NAME", "fesnyng_session"),
        cookie_secure=not local_development,
        allowed_origin=os.environ.get("FESNYNG_CONTROL_PLANE_ALLOWED_ORIGIN"),
    )
