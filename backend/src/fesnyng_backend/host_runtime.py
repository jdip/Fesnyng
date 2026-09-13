"""Docker and native OpenCode boundary; no replacement agent execution loop."""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import httpx

from fesnyng_backend.host_models import HostAgentConfiguration, permission_rules
from fesnyng_backend.host_store import HostStore


class RuntimeUnavailable(RuntimeError):
    pass


_WORKSPACE_GUIDANCE = (
    "Use the working directory in the current native environment as this thread's workspace. "
    "Historical absolute paths and tool workdirs may refer to a parent; resolve this thread's work "
    "under the current working directory and pass it to tools unless the current task explicitly "
    "requires another location."
)


class DockerRuntime:
    def __init__(self, store: HostStore, credential_url: str, image: str = "fesnyng-agent:local"):
        self.store = store
        self.credential_url = credential_url
        self.image = image
        self.locks: dict[str, asyncio.Lock] = {}

    def lock(self, agent_id: str) -> asyncio.Lock:
        return self.locks.setdefault(agent_id, asyncio.Lock())

    def name(self, agent_id: str) -> str:
        return f"fesnyng-{self.store.instance_id}-{agent_id}"

    async def docker(self, *args: str, content: bytes | None = None) -> bytes:
        def execute() -> bytes:
            try:
                result = subprocess.run(
                    ["docker", *args], input=content, capture_output=True, timeout=120, check=False
                )
            except (OSError, subprocess.TimeoutExpired):
                raise RuntimeUnavailable(
                    "Docker operation unavailable; check host operations"
                ) from None
            if result.returncode:
                # Docker arguments/output may contain runtime credentials or private tool output.
                raise RuntimeUnavailable(
                    "Docker operation failed; host resources retained for inspection"
                )
            return result.stdout

        return await asyncio.to_thread(execute)

    async def inspect(self, organization_id: str, agent_id: str) -> dict[str, Any] | None:
        name = self.name(agent_id)
        listed = (
            (
                await self.docker(
                    "container", "ls", "-a", "--filter", f"name=^/{name}$", "--format", "{{.Names}}"
                )
            )
            .decode()
            .splitlines()
        )
        if name not in listed:
            return None
        template = '{"labels":{{json .Config.Labels}},"state":{{json .State}},"ports":{{json .NetworkSettings.Ports}}}'
        info = json.loads(await self.docker("inspect", "--format", template, name))
        expected = {
            "fesnyng.host": str(self.store.instance_id),
            "fesnyng.organization": organization_id,
            "fesnyng.agent": agent_id,
        }
        if any(info["labels"].get(key) != value for key, value in expected.items()):
            raise RuntimeUnavailable("Container ownership mismatch; resource retained")
        return info

    async def ensure(self, organization_id: str, agent_id: str) -> None:
        agent = self.store.agent(organization_id, agent_id)
        info = await self.inspect(organization_id, agent_id)
        if info is None:
            if agent["applied_envelope"] and (
                not agent["snapshot_image"] or agent["runtime_state"] != "replacing"
            ):
                raise RuntimeUnavailable(
                    "Agent container is missing without a checkpoint; inspect retained state before replacement"
                )
            name = self.name(agent_id)
            for suffix in ("home", "workspace"):
                volume = f"{name}-{suffix}"
                present = (
                    (
                        await self.docker(
                            "volume", "ls", "--filter", f"name=^{volume}$", "--format", "{{.Name}}"
                        )
                    )
                    .decode()
                    .splitlines()
                )
                if volume in present:
                    labels = (
                        json.loads(
                            await self.docker(
                                "volume", "inspect", "--format", "{{json .Labels}}", volume
                            )
                        )
                        or {}
                    )
                    if labels.get("fesnyng.agent") != agent_id or labels.get("fesnyng.host") != str(
                        self.store.instance_id
                    ):
                        raise RuntimeUnavailable("Volume ownership mismatch; resource retained")
                else:
                    await self.docker(
                        "volume",
                        "create",
                        "--label",
                        f"fesnyng.agent={agent_id}",
                        "--label",
                        f"fesnyng.host={self.store.instance_id}",
                        volume,
                    )
            await self.docker(
                "run",
                "-d",
                "--name",
                name,
                "--label",
                f"fesnyng.host={self.store.instance_id}",
                "--label",
                f"fesnyng.organization={organization_id}",
                "--label",
                f"fesnyng.agent={agent_id}",
                "--security-opt",
                "no-new-privileges",
                "--memory",
                "1g",
                "--cpus",
                "2",
                "--pids-limit",
                "512",
                "--restart",
                "unless-stopped",
                "-p",
                "127.0.0.1::4096",
                "-v",
                f"{name}-home:/home/agent",
                "-v",
                f"{name}-workspace:/workspace",
                "-e",
                f"OPENCODE_SERVER_PASSWORD={agent['runtime_password']}",
                "-e",
                "FESNYNG_AGENT_AUTH=/home/agent/host-auth.json",
                "-e",
                'OPENCODE_AUTH_CONTENT={"openai":{"type":"oauth","access":"","refresh":"","expires":0}}',
                agent["snapshot_image"] or self.image,
            )
        elif not info["state"]["Running"]:
            await self.docker("start", self.name(agent_id))
        for _ in range(120):
            try:
                await self.request(organization_id, agent_id, "/global/health")
                return
            except (RuntimeUnavailable, httpx.HTTPError):
                await asyncio.sleep(0.5)
        raise RuntimeUnavailable("Native runtime startup timed out")

    async def request(
        self,
        organization_id: str,
        agent_id: str,
        path: str,
        *,
        method: str = "GET",
        body: Any = None,
        directory: str | None = None,
    ) -> Any:
        agent = self.store.agent(organization_id, agent_id)
        info = await self.inspect(organization_id, agent_id)
        if info is None or not info["state"]["Running"]:
            raise RuntimeUnavailable("Agent container is not running")
        ports = info["ports"].get("4096/tcp") or []
        if not ports or ports[0]["HostIp"] != "127.0.0.1":
            raise RuntimeUnavailable("Unexpected native runtime port binding")
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{int(ports[0]['HostPort'])}",
            auth=("opencode", agent["runtime_password"]),
            timeout=120,
        ) as client:
            try:
                response = await client.request(
                    method,
                    path,
                    json=body,
                    params={"directory": directory} if directory else None,
                )
            except httpx.HTTPError:
                raise RuntimeUnavailable("Native runtime connection unavailable") from None
            if not response.is_success:
                raise RuntimeUnavailable(
                    f"Native runtime request failed (HTTP {response.status_code})"
                )
            try:
                return response.json() if response.content else None
            except ValueError:
                raise RuntimeUnavailable("Native runtime returned an invalid response") from None

    async def request_with_query(
        self,
        organization_id: str,
        agent_id: str,
        path: str,
        query: dict[str, str],
        *,
        directory: str,
    ) -> Any:
        """Native GET with a finite caller-owned query contract."""
        agent = self.store.agent(organization_id, agent_id)
        info = await self.inspect(organization_id, agent_id)
        if info is None or not info["state"]["Running"]:
            raise RuntimeUnavailable("Agent container is not running")
        ports = info["ports"].get("4096/tcp") or []
        if not ports or ports[0]["HostIp"] != "127.0.0.1":
            raise RuntimeUnavailable("Unexpected native runtime port binding")
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{int(ports[0]['HostPort'])}",
            auth=("opencode", agent["runtime_password"]),
            timeout=120,
        ) as client:
            try:
                response = await client.get(path, params={**query, "directory": directory})
            except httpx.HTTPError:
                raise RuntimeUnavailable("Native runtime connection unavailable") from None
            if not response.is_success:
                raise RuntimeUnavailable(
                    f"Native runtime request failed (HTTP {response.status_code})"
                )
            try:
                return response.json() if response.content else None
            except ValueError:
                raise RuntimeUnavailable("Native runtime returned an invalid response") from None

    @asynccontextmanager
    async def event_stream(
        self, organization_id: str, agent_id: str, directory: str
    ) -> AsyncIterator[AsyncIterator[str]]:
        """Open one authenticated native SSE stream for an already-mapped workspace."""
        agent = self.store.agent(organization_id, agent_id)
        info = await self.inspect(organization_id, agent_id)
        if info is None or not info["state"]["Running"]:
            raise RuntimeUnavailable("Agent container is not running")
        ports = info["ports"].get("4096/tcp") or []
        if not ports or ports[0]["HostIp"] != "127.0.0.1":
            raise RuntimeUnavailable("Unexpected native runtime port binding")
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{int(ports[0]['HostPort'])}",
            auth=("opencode", agent["runtime_password"]),
            timeout=None,
        ) as client:
            try:
                async with client.stream(
                    "GET", "/event", params={"directory": directory}
                ) as response:
                    if not response.is_success or not response.headers.get(
                        "content-type", ""
                    ).startswith("text/event-stream"):
                        raise RuntimeUnavailable("Native event stream is unavailable")
                    yield response.aiter_lines()
            except httpx.HTTPError:
                raise RuntimeUnavailable("Native runtime connection unavailable") from None

    async def workspace_path(
        self, organization_id: str, agent_id: str, directory: str, path: str
    ) -> str:
        """Resolve an artifact path in-container and reject symlink/workspace escapes."""
        if path.startswith("/") or any(part in {"", ".", ".."} for part in path.split("/")):
            raise ValueError("Artifact path must be relative to its mapped workspace")
        await self.inspect(organization_id, agent_id)
        target = await self.docker(
            "exec",
            self.name(agent_id),
            "sh",
            "-c",
            'base="$(realpath -- "$1")" || exit 1; target="$(realpath -- "$base/$2")" || exit 1; case "$target" in "$base"/*) printf %s "$target";; *) exit 1;; esac',
            "workspace-path",
            directory,
            path,
        )
        resolved = target.decode()
        if not resolved:
            raise RuntimeUnavailable("Artifact path is unavailable")
        return resolved

    async def fork_workspace(
        self, organization_id: str, agent_id: str, source_directory: str
    ) -> str:
        """Copy one mapped checkout into an isolated writable native fork directory."""
        if not source_directory.startswith("/workspace/"):
            raise ValueError("Native session workspace is not managed by this agent")
        destination = f"{source_directory}-fork-{uuid4().hex}"
        await self.inspect(organization_id, agent_id)
        await self.docker(
            "exec",
            self.name(agent_id),
            "sh",
            "-c",
            r'test -d "$1" && test ! -L "$1" && ! find "$1" -name .git \( -type f -o -type l \) -print -quit | grep -q . && ! find "$1" -type l -print -quit | grep -q . && test ! -e "$2" && mkdir -p "$2" && cp -a "$1/." "$2/"',
            "fork-workspace",
            source_directory,
            destination,
        )
        return destination

    async def write_file(
        self, organization_id: str, agent_id: str, path: str, content: str
    ) -> None:
        await self.inspect(organization_id, agent_id)
        await self.docker(
            "exec",
            "-i",
            self.name(agent_id),
            "sh",
            "-c",
            'umask 077; mkdir -p "$(dirname "$1")"; cat > "$1"',
            "write",
            path,
            content=content.encode(),
        )

    async def assert_quiet(self, organization_id: str, agent_id: str) -> None:
        info = await self.inspect(organization_id, agent_id)
        if info is None or not info["state"]["Running"]:
            return
        directories = {
            session["directory"] for session in self.store.sessions(organization_id, agent_id)
        }
        for directory in [None, *sorted(directories)]:
            statuses = await self.request(
                organization_id, agent_id, "/session/status", directory=directory
            )
            if any(status.get("type") != "idle" for status in statuses.values()):
                raise RuntimeUnavailable("Configuration pending: native work is active")

    async def configure(self, envelope: HostAgentConfiguration) -> None:
        org, agent_id = str(envelope.organization_id), str(envelope.agent_id)
        agent = self.store.agent(org, agent_id)
        await self.ensure(org, agent_id)
        await self.assert_quiet(org, agent_id)
        configuration = envelope.configuration
        current = await self.request(org, agent_id, "/global/config")
        plugin = "file:///opt/fesnyng/host-auth.mjs"
        plugins = list(current.get("plugin", []))
        if not any((entry[0] if isinstance(entry, list) else entry) == plugin for entry in plugins):
            plugins.append(plugin)
        auth = {
            "broker": self.credential_url,
            "key": agent["agent_token"],
            "profile_id": str(configuration.profile_id)
            if configuration.profile_id
            else "unassigned",
        }
        config = {
            "plugin": plugins,
            "model": f"{configuration.provider}/{configuration.model}",
            "permission": envelope.policy.default_permission,
            "instructions": [
                *dict.fromkeys([*current.get("instructions", []), "/home/agent/AGENTS.md"])
            ],
            "skills": {
                "paths": [
                    *dict.fromkeys(
                        [*current.get("skills", {}).get("paths", []), "/home/agent/fesnyng-skills"]
                    )
                ]
            },
            "mcp": {
                "fesnyng": {
                    "type": "remote",
                    "url": self.credential_url.rstrip("/") + "/mcp/",
                    "headers": {"Authorization": f"Bearer {agent['agent_token']}"},
                    "oauth": False,
                }
            },
            "autoupdate": False,
            "share": "disabled",
            "enabled_providers": ["openai"],
        }
        await self.write_file(org, agent_id, "/home/agent/host-auth.json", json.dumps(auth))
        managed_instructions = _WORKSPACE_GUIDANCE
        if configuration.instructions:
            managed_instructions += f"\n\n{configuration.instructions}"
        await self.write_file(org, agent_id, "/home/agent/AGENTS.md", managed_instructions)
        # Only this directory contains Fesnyng-managed native skill assignments.
        await self.docker("exec", self.name(agent_id), "rm", "-rf", "/home/agent/fesnyng-skills")
        await self.docker(
            "exec",
            self.name(agent_id),
            "rm",
            "-rf",
            "/home/agent/.config/opencode/commands/fesnyng",
        )
        for skill in configuration.skills:
            if skill.explicit_only:
                path = f"/home/agent/.config/opencode/commands/fesnyng/{skill.name}.md"
                content = f"---\ndescription: {skill.name}\n---\n{skill.content}"
            else:
                path = f"/home/agent/fesnyng-skills/{skill.name}/SKILL.md"
                content = (
                    f"---\nname: {skill.name}\ndescription: {skill.name}\n---\n{skill.content}"
                )
            await self.write_file(org, agent_id, path, content)
        # The server caches global configuration. Its native update endpoint owns
        # both persistence and cache invalidation; writing JSON alone is insufficient.
        await self.request(org, agent_id, "/global/config", method="PATCH", body=config)
        directories = {session["directory"] for session in self.store.sessions(org, agent_id)}
        for directory in [None, *sorted(directories)]:
            await self.request(
                org, agent_id, "/instance/dispose", method="POST", body={}, directory=directory
            )
        effective = await self.request(org, agent_id, "/config")
        effective_plugins = [
            entry[0] if isinstance(entry, list) else entry for entry in effective.get("plugin", [])
        ]
        if effective.get("model") != config["model"] or plugin not in effective_plugins:
            raise RuntimeUnavailable("Native configuration was not applied; desired state retained")

    async def replace(self, organization_id: str, agent_id: str) -> None:
        async with self.lock(agent_id):
            info = await self.inspect(organization_id, agent_id)
            if info is None:
                raise RuntimeUnavailable(
                    "Missing container cannot be checkpointed; retained state needs inspection"
                )
            if info["state"]["Running"]:
                await self.assert_quiet(organization_id, agent_id)
                self.store.set_runtime_state(organization_id, agent_id, "checkpointing")
                await self.docker("stop", "--time", "30", self.name(agent_id))
            else:
                self.store.set_runtime_state(organization_id, agent_id, "checkpointing")
            image = f"fesnyng-checkpoint:{agent_id}-{uuid4().hex}"
            await self.docker("commit", self.name(agent_id), image)
            with self.store.connect() as connection:
                connection.execute(
                    "UPDATE host_agents SET snapshot_image=?,runtime_state='replacing' WHERE organization_id=? AND agent_id=?",
                    (image, organization_id, agent_id),
                )
            await self.docker("rm", self.name(agent_id))
            await self.ensure(organization_id, agent_id)
            # The checkpoint belongs to this replacement only. Retain its image
            # reference, but never use it to recover a later missing container.
            self.store.set_runtime_state(organization_id, agent_id, "running")

    async def create_session(
        self,
        organization_id: str,
        agent_id: str,
        title: str,
        workspace: str,
        *,
        directory: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        agent = self.store.agent(organization_id, agent_id)
        if not agent["applied_envelope"]:
            raise RuntimeUnavailable("Agent configuration is not applied")
        envelope = HostAgentConfiguration.model_validate_json(agent["applied_envelope"])
        if workspace != envelope.configuration.workspace:
            raise ValueError("Workspace is not assigned to this agent")
        directory = directory or f"/workspace/{workspace}/threads/{uuid4().hex}"
        await self.docker("exec", self.name(agent_id), "mkdir", "-p", directory)
        session = await self.request(
            organization_id,
            agent_id,
            "/session",
            method="POST",
            body={
                "title": title,
                "permission": permission_rules(envelope),
                **({"metadata": metadata} if metadata else {}),
            },
            directory=directory,
        )
        self.store.save_session(organization_id, agent_id, session["id"], directory, title)
        return session
