"""An explicit, organization-dedicated Docker engine grant for host services."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from fesnyng_backend.host_runtime import RuntimeUnavailable
from fesnyng_backend.host_store import HostStore
from fesnyng_backend.settings import DockerCapabilitySettings


class DockerCapabilityRuntime(Protocol):
    """Narrow Docker boundary used by resource registration and inspection."""

    async def docker_engine_id(self, socket_path: Path | None = None) -> str: ...

    async def docker_capability_command(self, socket_path: Path, *args: str) -> bytes: ...

    async def docker_capability_foreign_organizations(
        self, socket_path: Path, organization_id: str
    ) -> set[str]: ...


class DockerCapabilityStatus(BaseModel):
    """Sanitized capability state safe to return from host-facing APIs."""

    model_config = ConfigDict(frozen=True)

    enabled: bool
    available: bool
    reason: str | None
    engine_id: str | None


class DockerCapability:
    """Admit only a configured organization's allowed employees to one engine."""

    def __init__(self, store: HostStore, runtime: DockerCapabilityRuntime):
        self.store = store
        self.runtime = runtime

    @property
    def settings(self) -> DockerCapabilitySettings | None:
        return self.store.settings.docker_capability

    async def status(
        self, organization_id: str, agent_id: str | None = None
    ) -> DockerCapabilityStatus:
        configured = self.settings
        if configured is None:
            return DockerCapabilityStatus(
                enabled=False,
                available=False,
                reason="Docker capability is not configured",
                engine_id=None,
            )
        try:
            await self.require(organization_id, agent_id)
        except RuntimeUnavailable as error:
            return DockerCapabilityStatus(
                enabled=True,
                available=False,
                reason=str(error),
                engine_id=configured.engine_id,
            )
        return DockerCapabilityStatus(
            enabled=True,
            available=True,
            reason=None,
            engine_id=configured.engine_id,
        )

    async def require(
        self, organization_id: str, agent_id: str | None = None
    ) -> DockerCapabilityStatus:
        configured = self.settings
        if configured is None:
            raise RuntimeUnavailable("Docker capability is not configured")
        if str(configured.organization_id) != organization_id:
            raise RuntimeUnavailable("Docker capability belongs to a dedicated organization")
        if agent_id is not None and uuid_text(agent_id) not in {
            str(employee_id) for employee_id in configured.employee_ids
        }:
            raise RuntimeUnavailable("Employee is not allowed to use Docker capability")
        await self._require_engine(configured)
        foreign_organizations = await self.runtime.docker_capability_foreign_organizations(
            configured.socket_path, organization_id
        )
        if foreign_organizations:
            raise RuntimeUnavailable(
                "Docker engine has Fesnyng resources for another organization; resources retained"
            )
        return DockerCapabilityStatus(
            enabled=True,
            available=True,
            reason=None,
            engine_id=configured.engine_id,
        )

    async def command(self, organization_id: str, *args: str) -> bytes:
        """Run an argv-only Docker command after fresh organization admission."""
        configured = self.settings
        await self.require(organization_id)
        if configured is None:  # pragma: no cover - require always raises first
            raise RuntimeUnavailable("Docker capability is not configured")
        return await self.runtime.docker_capability_command(configured.socket_path, *args)

    async def _require_engine(self, configured: DockerCapabilitySettings) -> None:
        try:
            default_engine = await self.runtime.docker_engine_id()
            socket_engine = await self.runtime.docker_engine_id(configured.socket_path)
        except RuntimeUnavailable:
            raise RuntimeUnavailable("Configured Docker engine is unavailable") from None
        if default_engine != configured.engine_id or socket_engine != configured.engine_id:
            raise RuntimeUnavailable("Configured Docker engine is unavailable")


def uuid_text(value: str) -> str:
    """Reject malformed employee identifiers before comparing configuration UUIDs."""
    try:
        from uuid import UUID

        return str(UUID(value))
    except ValueError:
        return value
