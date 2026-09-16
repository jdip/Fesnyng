"""Host-owned service links for employees and registered external resources."""

from __future__ import annotations

import asyncio
import json
import posixpath
from typing import Any, Literal, Protocol
from urllib.parse import unquote, urlsplit
from uuid import UUID, uuid4

from pydantic import Field, field_validator

from fesnyng_backend.agent_models import Contract, Name
from fesnyng_backend.host_docker_resources import DockerResources, ResourceThread
from fesnyng_backend.host_runtime import RuntimeUnavailable
from fesnyng_backend.host_store import HostStore
from fesnyng_backend.settings import TailscaleServeSettings


class ServiceAssociations(Contract):
    name: Name
    threads: list[ResourceThread] = Field(default_factory=list, max_length=100)
    project_ids: list[UUID] = Field(default_factory=list, max_length=100)


class ServiceRegistration(ServiceAssociations):
    target_kind: Literal["employee", "resource"]
    target_id: UUID
    endpoint_url: str = Field(min_length=1, max_length=2_000)
    route: Literal["custom", "tailscale"]

    @field_validator("endpoint_url")
    @classmethod
    def safe_endpoint(cls, value: str) -> str:
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError as error:
            raise ValueError("Service endpoint port is invalid") from error
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Service endpoint must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError("Service endpoint must not contain credentials or a fragment")
        if parsed.hostname.lower() in {"docker", "docker.sock"}:
            raise ValueError("Service endpoint must not name a daemon endpoint")
        if port in {2375, 2376, 4096}:
            raise ValueError("Service endpoint must not target a daemon or native agent port")
        return value


class ServiceUpdate(ServiceRegistration):
    expected_revision: int = Field(ge=1)


class ServiceExpectation(Contract):
    expected_revision: int = Field(ge=1)


class TailscaleServe(Protocol):
    async def status(self, organization_id: str, endpoint_url: str) -> dict[str, str]: ...


class EmployeeRuntime(Protocol):
    """Owner-verified employee container inspection, independent of raw Docker grants."""

    async def inspect(self, organization_id: str, agent_id: str) -> dict[str, Any] | None: ...


class UnavailableTailscaleServe:
    async def status(self, organization_id: str, endpoint_url: str) -> dict[str, str]:
        return {"status": "unavailable", "reason": "Tailscale Serve status is unavailable"}


class ConfiguredTailscaleServe:
    """Read a matching Serve handler only; never return or modify the full map."""

    def __init__(self, settings: TailscaleServeSettings | None):
        self.settings = settings

    @staticmethod
    def _matching_handler(handlers: dict[str, Any], raw_path: str) -> bool:
        """Mirror Serve's decoded-path lookup followed by cleaned ancestors."""
        try:
            decoded = unquote(raw_path or "/", errors="strict")
        except UnicodeDecodeError:
            return False
        if not decoded.startswith("/"):
            return False
        clean = posixpath.normpath(decoded)
        # Go's path.Clean reduces all leading slashes to one; posixpath keeps two.
        clean = "/" + clean.lstrip("/")
        if not clean.startswith("/"):
            return False
        while True:
            if clean in handlers or clean + "/" in handlers:
                return True
            if clean == "/":
                return False
            parent = posixpath.dirname(clean)
            if parent == clean:
                return False
            clean = parent

    async def status(self, organization_id: str, endpoint_url: str) -> dict[str, str]:
        if self.settings is None or str(self.settings.organization_id) != organization_id:
            return {"status": "unavailable", "reason": "Tailscale Serve is not configured"}
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *self.settings.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5)
            if process.returncode != 0 or len(stdout) > 1_000_000:
                raise RuntimeUnavailable("Tailscale Serve status is unavailable")
            status = json.loads(stdout)
        except (OSError, ValueError, TimeoutError, RuntimeUnavailable, TypeError):
            return {"status": "unavailable", "reason": "Tailscale Serve status is unavailable"}
        except asyncio.CancelledError:
            raise
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
        if not isinstance(status, dict):
            return {"status": "unavailable", "reason": "Tailscale Serve status is unavailable"}
        parsed = urlsplit(endpoint_url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        web, tcp = status.get("Web"), status.get("TCP")
        if not isinstance(web, dict) or not isinstance(tcp, dict):
            return {"status": "unavailable", "reason": "Tailscale Serve status is unavailable"}
        route, tcp_route = web.get(f"{parsed.hostname}:{port}"), tcp.get(str(port))
        if not isinstance(route, dict) or not isinstance(tcp_route, dict):
            return {
                "status": "unavailable",
                "reason": "No matching Tailscale Serve route is configured",
            }
        funnel = route.get("AllowFunnel")
        if funnel is None:
            global_funnel = status.get("AllowFunnel")
            funnel = (
                global_funnel.get(f"{parsed.hostname}:{port}")
                if isinstance(global_funnel, dict)
                else global_funnel
            )
        if funnel is True:
            return {
                "status": "unavailable",
                "reason": "Matching route allows Funnel and is not private",
            }
        https, http = tcp_route.get("HTTPS"), tcp_route.get("HTTP")
        if (
            not all(value is None or isinstance(value, bool) for value in (https, http))
            or (parsed.scheme == "https" and (https is not True or http is True))
            or (parsed.scheme == "http" and (http is not True or https is True))
        ):
            return {
                "status": "unavailable",
                "reason": "Tailscale Serve protocol does not match endpoint",
            }
        handlers = route.get("Handlers")
        if isinstance(handlers, dict) and self._matching_handler(handlers, parsed.path):
            return {"status": "configured"}
        return {
            "status": "unavailable",
            "reason": "No matching Tailscale Serve route is configured",
        }


class DockerServices:
    """Keep service metadata and associations without provisioning networking."""

    def __init__(
        self,
        host: HostStore,
        resources: DockerResources,
        runtime: EmployeeRuntime,
        tailscale: TailscaleServe | None = None,
    ):
        self.host = host
        self.resources = resources
        self.runtime = runtime
        self.tailscale = tailscale or UnavailableTailscaleServe()
        self._lock = asyncio.Lock()

    def initialize(self) -> None:
        with self.host.connect() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS host_docker_services (
                id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES host_bindings(organization_id),
                name TEXT NOT NULL, target_kind TEXT NOT NULL CHECK(target_kind IN ('employee','resource')),
                target_id TEXT NOT NULL, endpoint_url TEXT NOT NULL,
                route TEXT NOT NULL CHECK(route IN ('custom','tailscale')),
                threads TEXT NOT NULL, project_ids TEXT NOT NULL, revision INTEGER NOT NULL
            )""")

    def _validate_added_associations(
        self, org: str, body: ServiceAssociations, existing: dict[str, Any] | None = None
    ) -> tuple[str, str]:
        previous = {
            (thread["agent_id"], thread["session_id"])
            for thread in (existing or {}).get("threads", [])
        }
        for thread in body.threads:
            if (str(thread.agent_id), thread.session_id) not in previous:
                self.host.session(org, str(thread.agent_id), thread.session_id)
        threads = list(
            {
                (str(item.agent_id), item.session_id): item.model_dump(mode="json")
                for item in body.threads
            }.values()
        )
        return json.dumps(threads), json.dumps(
            sorted({str(project) for project in body.project_ids})
        )

    def _record(self, org: str, service_id: str) -> dict[str, Any]:
        with self.host.connect() as connection:
            row = connection.execute(
                "SELECT * FROM host_docker_services WHERE organization_id=? AND id=?",
                (org, service_id),
            ).fetchone()
        if row is None:
            raise LookupError("Registered service not found")
        result = dict(row)
        result.pop("organization_id")
        result["threads"] = json.loads(result["threads"])
        result["project_ids"] = json.loads(result["project_ids"])
        return result

    async def _target(self, org: str, item: dict[str, Any]) -> dict[str, Any]:
        target_id = item["target_id"]
        if item["target_kind"] == "employee":
            try:
                self.host.agent(org, target_id)
            except LookupError:
                return {"kind": "employee", "id": target_id, "status": "missing"}
            try:
                container = await self.runtime.inspect(org, target_id)
            except RuntimeUnavailable as error:
                return {
                    "kind": "employee",
                    "id": target_id,
                    "status": "unavailable",
                    "reason": str(error),
                }
            if container is None:
                return {"kind": "employee", "id": target_id, "status": "missing"}
            try:
                running = container["state"]["Running"]
            except (KeyError, TypeError):
                return {
                    "kind": "employee",
                    "id": target_id,
                    "status": "unavailable",
                    "reason": "Employee container inspection is unavailable",
                }
            return {
                "kind": "employee",
                "id": target_id,
                "status": "available",
                "running": bool(running),
            }
        try:
            resource = await self.resources.inspect(org, target_id)
        except LookupError:
            return {"kind": "resource", "id": target_id, "status": "missing"}
        inspection = resource["inspection"]
        target = {"kind": "resource", "id": target_id, "status": inspection["status"]}
        if inspection["status"] == "available":
            target["running"] = inspection["running"]
        elif inspection.get("reason"):
            target["reason"] = inspection["reason"]
        return target

    async def _route_status(self, org: str, item: dict[str, Any]) -> dict[str, str]:
        if item["route"] == "custom":
            return {
                "status": "configured",
                "reason": "Custom route is registered; network reachability is not checked",
                "network_reachability": "unverified",
            }
        status = await self.tailscale.status(org, item["endpoint_url"])
        return {**status, "network_reachability": "unverified"}

    async def _validate_target(self, org: str, body: ServiceRegistration) -> None:
        if body.target_kind == "employee":
            self.host.agent(org, str(body.target_id))
        else:
            # Registration is allowed when raw Docker access is disabled; the
            # public inspection reports that retained target as unavailable.
            await self.resources.inspect(org, str(body.target_id))

    async def register(self, org: str, body: ServiceRegistration) -> dict[str, Any]:
        async with self._lock:
            await self._validate_target(org, body)
            threads, projects = self._validate_added_associations(org, body)
            service_id = str(uuid4())
            with self.host.connect() as connection:
                connection.execute(
                    "INSERT INTO host_docker_services VALUES(?,?,?,?,?,?,?,?,?,1)",
                    (
                        service_id,
                        org,
                        body.name,
                        body.target_kind,
                        str(body.target_id),
                        body.endpoint_url,
                        body.route,
                        threads,
                        projects,
                    ),
                )
            return await self.inspect(org, service_id)

    async def inspect(self, org: str, service_id: str) -> dict[str, Any]:
        item = self._record(org, service_id)
        item["target"] = await self._target(org, item)
        item["route_status"] = await self._route_status(org, item)
        return item

    async def inventory(self, org: str) -> list[dict[str, Any]]:
        with self.host.connect() as connection:
            ids = [
                row[0]
                for row in connection.execute(
                    "SELECT id FROM host_docker_services WHERE organization_id=? ORDER BY name,id",
                    (org,),
                )
            ]
        return [await self.inspect(org, item) for item in ids]

    async def update(self, org: str, service_id: str, body: ServiceUpdate) -> dict[str, Any]:
        async with self._lock:
            current = self._record(org, service_id)
            if current["revision"] != body.expected_revision:
                raise ValueError("Service associations changed; inspect and confirm again")
            if (body.target_kind, str(body.target_id)) != (
                current["target_kind"],
                current["target_id"],
            ):
                await self._validate_target(org, body)
            threads, projects = self._validate_added_associations(org, body, current)
            with self.host.connect() as connection:
                connection.execute(
                    """UPDATE host_docker_services SET name=?,target_kind=?,target_id=?,endpoint_url=?,
                    route=?,threads=?,project_ids=?,revision=revision+1 WHERE organization_id=? AND id=?""",
                    (
                        body.name,
                        body.target_kind,
                        str(body.target_id),
                        body.endpoint_url,
                        body.route,
                        threads,
                        projects,
                        org,
                        service_id,
                    ),
                )
            return await self.inspect(org, service_id)

    async def unregister(
        self, org: str, service_id: str, expected_revision: int
    ) -> dict[str, bool]:
        async with self._lock:
            current = self._record(org, service_id)
            if current["revision"] != expected_revision:
                raise ValueError("Service associations changed; inspect and confirm again")
            with self.host.connect() as connection:
                connection.execute(
                    "DELETE FROM host_docker_services WHERE organization_id=? AND id=?",
                    (org, service_id),
                )
            return {"removed": True}
