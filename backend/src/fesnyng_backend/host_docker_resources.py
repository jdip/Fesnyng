"""Host-owned registration of shared external containers, never stack orchestration."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from pydantic import Field

from fesnyng_backend.agent_models import Contract, Name
from fesnyng_backend.host_models import NativeID
from fesnyng_backend.host_runtime import RuntimeUnavailable
from fesnyng_backend.host_store import HostStore


class ResourceThread(Contract):
    agent_id: UUID
    session_id: NativeID


class ResourceAssociations(Contract):
    name: Name
    threads: list[ResourceThread] = Field(default_factory=list, max_length=100)
    project_ids: list[UUID] = Field(default_factory=list, max_length=100)


class ResourceRegistration(ResourceAssociations):
    container_id: str = Field(pattern=r"^[0-9a-f]{64}$")


class ResourceUpdate(ResourceAssociations):
    expected_revision: int = Field(ge=1)


class ResourceExpectation(Contract):
    expected_revision: int = Field(ge=1)


class EngineReceipt(Protocol):
    @property
    def engine_id(self) -> str | None: ...


class ResourceEngine(Protocol):
    async def require(
        self, organization_id: str, /, agent_id: str | None = None
    ) -> EngineReceipt: ...

    async def command(self, organization_id: str, /, *args: str) -> bytes: ...


class DockerResources:
    def __init__(self, host: HostStore, engine: ResourceEngine):
        self.host = host
        self.engine = engine
        self._lock = asyncio.Lock()

    def initialize(self) -> None:
        with self.host.connect() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS host_docker_resources (
                id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES host_bindings(organization_id),
                engine_id TEXT NOT NULL, container_id TEXT NOT NULL, name TEXT NOT NULL,
                threads TEXT NOT NULL, project_ids TEXT NOT NULL, revision INTEGER NOT NULL,
                UNIQUE(organization_id,engine_id,container_id)
            )""")

    def maintenance_lock(self) -> asyncio.Lock:
        return self._lock

    def _associations(self, org: str, body: ResourceAssociations) -> tuple[str, str]:
        for thread in body.threads:
            self.host.session(org, str(thread.agent_id), thread.session_id)
        threads = list(
            {
                (str(t.agent_id), t.session_id): t.model_dump(mode="json") for t in body.threads
            }.values()
        )
        return json.dumps(threads), json.dumps(sorted({str(p) for p in body.project_ids}))

    def _record(self, org: str, resource_id: str) -> dict[str, Any]:
        with self.host.connect() as connection:
            row = connection.execute(
                "SELECT * FROM host_docker_resources WHERE organization_id=? AND id=?",
                (org, resource_id),
            ).fetchone()
        if row is None:
            raise LookupError("Registered resource not found")
        item = dict(row)
        item.pop("organization_id")
        item["threads"] = json.loads(item["threads"])
        item["project_ids"] = json.loads(item["project_ids"])
        return item

    async def _container(self, org: str, container_id: str) -> dict[str, Any] | None:
        ids = (
            (
                await self.engine.command(
                    org, "container", "ls", "--all", "--no-trunc", "--format", "{{.ID}}"
                )
            )
            .decode()
            .splitlines()
        )
        if container_id not in ids:
            return None
        try:
            rows = json.loads(await self.engine.command(org, "container", "inspect", container_id))
            item = rows[0]
            if item["Id"] != container_id:
                raise ValueError
            labels = item["Config"].get("Labels") or {}
            # Employee containers have their own lifecycle/history safeguards.
            if labels.get("fesnyng.host") or labels.get("fesnyng.agent"):
                raise PermissionError("Use the employee lifecycle for Fesnyng-managed containers")
            if labels.get("fesnyng.organization") not in (None, org):
                raise PermissionError("Container belongs to another organization")
            return item
        except (KeyError, IndexError, TypeError, ValueError):
            raise RuntimeUnavailable("Container inspection is unavailable") from None

    def _inspection(self, item: dict[str, Any]) -> dict[str, Any]:
        try:
            labels = item["Config"].get("Labels") or {}
            network = item["NetworkSettings"]
            return {
                "status": "available",
                "state": item["State"]["Status"],
                "running": item["State"]["Running"],
                "mounts": [
                    {
                        "type": m["Type"],
                        "source": m["Source"],
                        "destination": m["Destination"],
                        "read_only": not m["RW"],
                    }
                    for m in item["Mounts"]
                    if not self._socket_mount(m["Source"], m["Destination"])
                ],
                "ports": [
                    {
                        "container_port": port,
                        "host_ip": binding["HostIp"],
                        "host_port": binding["HostPort"],
                    }
                    for port, bindings in (network.get("Ports") or {}).items()
                    for binding in (bindings or [])
                ],
                "networks": sorted((network.get("Networks") or {}).keys()),
                "compose_project": labels.get("com.docker.compose.project"),
                "compose_service": labels.get("com.docker.compose.service"),
            }
        except (KeyError, TypeError, AttributeError):
            raise RuntimeUnavailable("Container inspection is unavailable") from None

    def _socket_mount(self, source: str, destination: str) -> bool:
        if source.endswith("docker.sock") or destination.endswith("docker.sock"):
            return True
        configured = self.host.settings.docker_capability
        if configured is None:
            return False
        try:
            return Path(source).resolve() == configured.socket_path.resolve()
        except (OSError, RuntimeError):
            # Uncertain mount metadata must not expose a possible daemon endpoint.
            return True

    async def register(self, org: str, body: ResourceRegistration) -> dict[str, Any]:
        async with self._lock:
            self.host.require_maintenance_open()
            receipt = await self.engine.require(org)
            threads, projects = self._associations(org, body)
            container = await self._container(org, body.container_id)
            if container is None:
                raise ValueError("Container is missing; discover its current identity")
            self._inspection(container)
            with self.host.connect() as connection:
                if connection.execute(
                    "SELECT 1 FROM host_docker_resources WHERE organization_id=? AND engine_id=? AND container_id=?",
                    (org, receipt.engine_id, body.container_id),
                ).fetchone():
                    raise ValueError(
                        "Container is already registered; edit its shared associations"
                    )
                resource_id = str(uuid4())
                connection.execute(
                    "INSERT INTO host_docker_resources VALUES(?,?,?,?,?,?,?,1)",
                    (
                        resource_id,
                        org,
                        receipt.engine_id,
                        body.container_id,
                        body.name,
                        threads,
                        projects,
                    ),
                )
            return await self.inspect(org, resource_id)

    async def inspect(self, org: str, resource_id: str) -> dict[str, Any]:
        item = self._record(org, resource_id)
        try:
            receipt = await self.engine.require(org)
            if receipt.engine_id != item["engine_id"]:
                raise RuntimeUnavailable("Registered engine has changed; resource retained")
            container = await self._container(org, item["container_id"])
            item["inspection"] = (
                self._inspection(container)
                if container
                else {"status": "missing", "reason": "Registered container no longer exists"}
            )
        except (RuntimeUnavailable, PermissionError) as error:
            item["inspection"] = {"status": "unavailable", "reason": str(error)}
        return item

    async def inventory(self, org: str) -> list[dict[str, Any]]:
        with self.host.connect() as connection:
            ids = [
                row[0]
                for row in connection.execute(
                    "SELECT id FROM host_docker_resources WHERE organization_id=? ORDER BY name,id",
                    (org,),
                )
            ]
        return [await self.inspect(org, resource_id) for resource_id in ids]

    async def discover(self, org: str) -> list[dict[str, Any]]:
        await self.engine.require(org)
        ids = (
            (
                await self.engine.command(
                    org, "container", "ls", "--all", "--no-trunc", "--format", "{{.ID}}"
                )
            )
            .decode()
            .splitlines()
        )
        found = []
        for container_id in ids:
            try:
                item = await self._container(org, container_id)
            except PermissionError:
                continue
            if item is None:
                continue
            detail = self._inspection(item)
            found.append(
                {
                    "container_id": container_id,
                    "name": str(item.get("Name", container_id)).lstrip("/"),
                    "state": detail["state"],
                    "compose_project": detail["compose_project"],
                    "compose_service": detail["compose_service"],
                }
            )
        return found

    async def update(self, org: str, resource_id: str, body: ResourceUpdate) -> dict[str, Any]:
        async with self._lock:
            self.host.require_maintenance_open()
            item = self._record(org, resource_id)
            if item["revision"] != body.expected_revision:
                raise ValueError("Resource associations changed; inspect and confirm again")
            threads, projects = self._associations(org, body)
            with self.host.connect() as connection:
                connection.execute(
                    "UPDATE host_docker_resources SET name=?,threads=?,project_ids=?,revision=revision+1 WHERE organization_id=? AND id=?",
                    (body.name, threads, projects, org, resource_id),
                )
            return await self.inspect(org, resource_id)

    async def operate(
        self,
        org: str,
        resource_id: str,
        expected_revision: int,
        action: Literal["start", "stop", "remove", "unregister"],
    ) -> dict[str, Any]:
        async with self._lock:
            self.host.require_maintenance_open()
            item = self._record(org, resource_id)
            if item["revision"] != expected_revision:
                raise ValueError("Resource associations changed; inspect and confirm again")
            if action == "unregister":
                with self.host.connect() as connection:
                    connection.execute(
                        "DELETE FROM host_docker_resources WHERE organization_id=? AND id=?",
                        (org, resource_id),
                    )
                return {"removed": True}
            receipt = await self.engine.require(org)
            if receipt.engine_id != item["engine_id"]:
                raise RuntimeUnavailable("Registered engine has changed; resource retained")
            container = await self._container(org, item["container_id"])
            if container is None:
                raise ValueError("Registered container is missing")
            inspection = self._inspection(container)
            if action == "remove" and inspection["running"]:
                raise ValueError("Container is running; explicitly stop it before removal")
            # An immutable container ID prevents name reuse from targeting a replacement.
            # No force flag or volume teardown: Docker also refuses removal if a
            # concurrent raw-engine user starts the container after this inspection.
            await self.engine.command(
                org, "rm" if action == "remove" else action, item["container_id"]
            )
            return await self.inspect(org, resource_id)
