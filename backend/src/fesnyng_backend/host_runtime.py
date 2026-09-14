"""Docker and native OpenCode boundary; no replacement agent execution loop."""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import httpx

from fesnyng_backend.host_models import HostAgentConfiguration, permission_rules
from fesnyng_backend.host_store import HostStore


class RuntimeUnavailable(RuntimeError):
    pass


def codex_thread_family(
    roots: set[str], threads: list[Mapping[str, Any]]
) -> list[Mapping[str, Any]]:
    """Validate native ancestry and select the roots and their descendants."""
    by_id: dict[str, Mapping[str, Any]] = {}
    for thread in threads:
        thread_id = thread.get("id")
        if not isinstance(thread_id, str) or not thread_id or thread_id in by_id:
            raise RuntimeUnavailable("Codex thread status receipt is invalid")
        parent = thread.get("parentThreadId")
        if parent is not None and not isinstance(parent, str):
            raise RuntimeUnavailable("Codex thread ancestry receipt is invalid")
        by_id[thread_id] = thread
    if not roots <= by_id.keys():
        raise RuntimeUnavailable("Codex mapped thread is missing from native status")
    related = set(roots)
    changed = True
    while changed:
        changed = False
        for thread_id, thread in by_id.items():
            if thread.get("parentThreadId") in related and thread_id not in related:
                related.add(thread_id)
                changed = True
    return [by_id[thread_id] for thread_id in sorted(related)]


def _assert_codex_threads_quiet(roots: set[str], threads: list[Mapping[str, Any]]) -> None:
    """Reject active native roots or descendants discovered from App Server."""
    for thread in codex_thread_family(roots, threads):
        status = thread.get("status")
        if not isinstance(status, Mapping) or not isinstance(status.get("type"), str):
            raise RuntimeUnavailable("Codex thread status receipt is invalid")
        if status["type"] == "active":
            raise RuntimeUnavailable("Configuration pending: native work is active")
        if status["type"] not in {"idle", "notLoaded"}:
            raise RuntimeUnavailable("Codex native thread status is uncertain")


class RuntimeRouter:
    """Resolve a thread's immutable harness binding to its native runtime.

    Keeping resolution next to the native boundary prevents a Codex binding
    from accidentally falling through to the OpenCode client.
    """

    def __init__(self, opencode: Any, codex: Any | None = None):
        self.opencode = opencode
        self.codex = codex

    def for_session(self, session: Mapping[str, Any]) -> Any:
        runtime_type = session.get("runtime_type")
        if runtime_type == "opencode":
            return self.opencode
        if runtime_type == "codex" and self.codex is not None:
            return self.codex
        if runtime_type == "codex":
            raise RuntimeUnavailable("Codex harness is not available on this host")
        raise RuntimeUnavailable("Thread harness binding is invalid")

    @staticmethod
    def require_supported(runtime_type: object) -> None:
        """Protect legacy OpenCode-only callers from a Codex fallthrough.

        New harness-aware owners resolve through :meth:`for_session`; callers
        still using this compatibility guard must remain OpenCode-only.
        """
        if runtime_type == "opencode":
            return
        if runtime_type == "codex":
            raise RuntimeUnavailable("Codex harness is not available on this host")
        raise RuntimeUnavailable("Thread harness binding is invalid")


def _workspace_context(result: bytes) -> dict[str, dict[str, int | str | None]]:
    """Validate the fixed, path-free context receipt from one container command."""
    try:
        fields = result.decode().split("\0")
    except UnicodeDecodeError:
        raise RuntimeUnavailable("Workspace context is unavailable") from None
    if not fields or fields.pop() != "" or len(fields) != 10:
        raise RuntimeUnavailable("Workspace context is unavailable")
    (
        repository_state,
        repository_name,
        branch_state,
        branch_name,
        changes_state,
        added,
        deleted,
        binary_files,
        untracked,
        reason,
    ) = fields
    if repository_state == "absent":
        if (
            any(
                value
                for value in (
                    repository_name,
                    branch_name,
                    added,
                    deleted,
                    binary_files,
                    untracked,
                    reason,
                )
            )
            or branch_state != "not_applicable"
            or changes_state != "not_applicable"
        ):
            raise RuntimeUnavailable("Workspace context is unavailable")
        return {
            "repository": {"state": "absent"},
            "branch": {"state": "not_applicable"},
            "changes": {"state": "not_applicable"},
        }
    if (
        repository_state != "available"
        or not repository_name
        or "/" in repository_name
        or branch_state != "available"
    ):
        raise RuntimeUnavailable("Workspace context is unavailable")
    repository: dict[str, int | str | None] = {"state": "available", "name": repository_name}
    branch: dict[str, int | str | None] = {
        "state": "available",
        "name": branch_name or None,
    }
    if changes_state == "unavailable" and reason in {"unborn", "configured_filter"}:
        if any((added, deleted, binary_files, untracked)):
            raise RuntimeUnavailable("Workspace context is unavailable")
        return {
            "repository": repository,
            "branch": branch,
            "changes": {"state": "unavailable", "reason": reason},
        }
    if (
        changes_state != "available"
        or reason
        or not all(value.isdecimal() for value in (added, deleted, binary_files, untracked))
    ):
        raise RuntimeUnavailable("Workspace context is unavailable")
    return {
        "repository": repository,
        "branch": branch,
        "changes": {
            "state": "available",
            "added": int(added),
            "deleted": int(deleted),
            "binaryFiles": int(binary_files),
            "untracked": int(untracked),
        },
    }


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
        self.native_ports: dict[tuple[str, str], int] = {}
        from fesnyng_backend.codex_runtime import CodexRuntime

        self.codex = CodexRuntime(self)
        self.runtime_router = RuntimeRouter(self, self.codex)

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

    def native_port(self, organization_id: str, agent_id: str) -> int:
        try:
            return self.native_ports[(organization_id, agent_id)]
        except KeyError:
            raise RuntimeUnavailable("Agent container is not running") from None

    async def running_port(self, organization_id: str, agent_id: str) -> int:
        """Inspect the native endpoint without starting or creating a container."""
        info = await self.inspect(organization_id, agent_id)
        if info is None or not info["state"]["Running"]:
            raise RuntimeUnavailable("Agent container is not running")
        port = self._port(info)
        self.native_ports[(organization_id, agent_id)] = port
        return port

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
            await self._create_container(
                organization_id,
                agent_id,
                agent["snapshot_image"] or self.image,
                require_volumes=False,
            )
        elif not info["state"]["Running"]:
            await self.docker("start", self.name(agent_id))
        info = await self.inspect(organization_id, agent_id)
        if info is None:
            raise RuntimeUnavailable("Agent container is not running")
        self.native_ports[(organization_id, agent_id)] = self._port(info)
        await self._wait_healthy(organization_id, agent_id)

    async def start(self, organization_id: str, agent_id: str) -> None:
        info = await self.inspect(organization_id, agent_id)
        if info is None:
            raise RuntimeUnavailable("Agent container is missing; rebuild is required")
        if not info["state"]["Running"]:
            await self.docker("start", self.name(agent_id))
        info = await self.inspect(organization_id, agent_id)
        if info is None:
            raise RuntimeUnavailable("Agent container is not running")
        self.native_ports[(organization_id, agent_id)] = self._port(info)
        await self._wait_healthy(organization_id, agent_id)

    async def stop(self, organization_id: str, agent_id: str) -> None:
        info = await self.inspect(organization_id, agent_id)
        if info is None:
            raise RuntimeUnavailable("Agent container is missing; rebuild is required")
        if info["state"]["Running"]:
            await self.docker("stop", "--time", "30", self.name(agent_id))

    async def restart(self, organization_id: str, agent_id: str) -> None:
        await self.stop(organization_id, agent_id)
        await self.start(organization_id, agent_id)

    async def rebuild(self, organization_id: str, agent_id: str) -> None:
        await self._require_retained_volumes(agent_id)
        info = await self.inspect(organization_id, agent_id)
        if info is not None:
            if info["state"]["Running"]:
                await self.docker("stop", "--time", "30", self.name(agent_id))
            await self.docker("rm", self.name(agent_id))
        await self._create_container(organization_id, agent_id, self.image, require_volumes=True)
        info = await self.inspect(organization_id, agent_id)
        if info is None:
            raise RuntimeUnavailable("Agent container is not running")
        self.native_ports[(organization_id, agent_id)] = self._port(info)
        await self._wait_healthy(organization_id, agent_id)

    async def switch_harness(self, organization_id: str, agent_id: str) -> None:
        """Replace the frozen harness under the configuration owner's runtime lock."""
        agent = self.store.agent(organization_id, agent_id)
        desired = HostAgentConfiguration.model_validate_json(agent["desired_envelope"])
        if not agent["applied_envelope"]:
            raise RuntimeUnavailable("Harness switch has no applied source configuration")
        previous = HostAgentConfiguration.model_validate_json(agent["applied_envelope"])
        if not self.store.harness_switch_frozen_for(
            organization_id, agent_id, previous.version, desired.configuration.runtime_type
        ):
            raise RuntimeUnavailable("Harness replacement requires a committed history freeze")
        await self.codex.transport.close_agent(organization_id, agent_id)
        await self.rebuild(organization_id, agent_id)

    async def _require_retained_volumes(self, agent_id: str) -> None:
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
            if volume not in present:
                raise RuntimeUnavailable("Retained agent volume is missing; resource retained")
            labels = (
                json.loads(
                    await self.docker("volume", "inspect", "--format", "{{json .Labels}}", volume)
                )
                or {}
            )
            if labels.get("fesnyng.agent") != agent_id or labels.get("fesnyng.host") != str(
                self.store.instance_id
            ):
                raise RuntimeUnavailable("Volume ownership mismatch; resource retained")

    async def _create_container(
        self,
        organization_id: str,
        agent_id: str,
        image: str,
        *,
        require_volumes: bool,
    ) -> None:
        agent = self.store.agent(organization_id, agent_id)
        desired = HostAgentConfiguration.model_validate_json(agent["desired_envelope"])
        runtime_type = desired.configuration.runtime_type
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
            elif require_volumes:
                raise RuntimeUnavailable("Retained agent volume is missing; resource retained")
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
            f"FESNYNG_RUNTIME_TYPE={runtime_type}",
            "-e",
            f"FESNYNG_RUNTIME_TOKEN={agent['runtime_password']}",
            "-e",
            f"FESNYNG_AGENT_TOKEN={agent['agent_token']}",
            "-e",
            f"FESNYNG_MCP_URL={self.credential_url.rstrip('/')}/mcp/",
            "-e",
            "FESNYNG_AGENT_AUTH=/home/agent/host-auth.json",
            "-e",
            'OPENCODE_AUTH_CONTENT={"openai":{"type":"oauth","access":"","refresh":"","expires":0}}',
            image,
        )

    @staticmethod
    def _port(info: Mapping[str, Any]) -> int:
        ports = info["ports"].get("4096/tcp") or []
        if not ports or ports[0].get("HostIp") != "127.0.0.1":
            raise RuntimeUnavailable("Unexpected native runtime port binding")
        try:
            return int(ports[0]["HostPort"])
        except (KeyError, TypeError, ValueError):
            raise RuntimeUnavailable("Unexpected native runtime port binding") from None

    async def _wait_healthy(self, organization_id: str, agent_id: str) -> None:
        agent = self.store.agent(organization_id, agent_id)
        envelope = HostAgentConfiguration.model_validate_json(agent["desired_envelope"])
        for _ in range(120):
            try:
                codex = envelope.configuration.runtime_type == "codex"
                port = self.native_port(organization_id, agent_id)
                path = "/readyz" if codex else "/global/health"
                # A socket accepted during startup can stall before HTTP is ready.
                # Health probes use a short timeout, unlike native execution calls.
                async with httpx.AsyncClient(
                    timeout=2, auth=None if codex else ("opencode", agent["runtime_password"])
                ) as client:
                    response = await client.get(f"http://127.0.0.1:{port}{path}")
                if response.status_code != 200:
                    raise RuntimeUnavailable("Native runtime is not ready")
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
        if path and (
            path.startswith("/") or any(part in {"", ".", ".."} for part in path.split("/"))
        ):
            raise ValueError("Artifact path must be relative to its mapped workspace")
        await self.inspect(organization_id, agent_id)
        target = await self.docker(
            "exec",
            self.name(agent_id),
            "sh",
            "-c",
            'base="$(realpath -- "$1")" || exit 1; test "$base" = "$1" || exit 1; target="$(realpath -- "$base/$2")" || exit 1; case "$target" in "$base"|"$base"/*) printf %s "$target";; *) exit 1;; esac',
            "workspace-path",
            directory,
            path,
        )
        resolved = target.decode()
        if not resolved:
            raise RuntimeUnavailable("Artifact path is unavailable")
        return resolved

    async def workspace_context(
        self, organization_id: str, agent_id: str, directory: str
    ) -> dict[str, dict[str, int | str | None]]:
        """Read only the mapped checkout's display-safe Git context."""
        await self.inspect(organization_id, agent_id)
        script = r'''base="$(realpath -- "$1")" || exit 1
test "$base" = "$1" && test -d "$base" || exit 1
if [ ! -e "$base/.git" ]; then
    printf "absent\0\0not_applicable\0\0not_applicable\0\0\0\0\0\0"
    exit 0
fi
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null
root="$(git --no-optional-locks -c core.fsmonitor=false -C "$base" rev-parse --show-toplevel 2>/dev/null)" || exit 1
test "$root" = "$base" || exit 1
git_dir="$(git --no-optional-locks -c core.fsmonitor=false -C "$base" rev-parse --absolute-git-dir 2>/dev/null)" || exit 1
common_dir="$(git --no-optional-locks -c core.fsmonitor=false -C "$base" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" || exit 1
git_dir="$(realpath -- "$git_dir")" || exit 1
common_dir="$(realpath -- "$common_dir")" || exit 1
case "$git_dir" in "$base"/.git|"$base"/.git/*) ;; *) exit 1;; esac
case "$common_dir" in "$base"/.git|"$base"/.git/*) ;; *) exit 1;; esac
name="${root##*/}"
if origin="$(git --no-optional-locks -c core.fsmonitor=false -C "$base" config --get remote.origin.url 2>/dev/null)"; then
    case "$origin" in
        https://*|http://*|ssh://*|git://*|*@*:*)
            candidate="${origin%/}"
            candidate="${candidate##*/}"
            case "$candidate" in *:*) candidate="${candidate##*:}";; esac
            candidate="${candidate%.git}"
            case "$candidate" in ""|*[!A-Za-z0-9._-]*) ;; *) name="$candidate";; esac
            ;;
    esac
else
    status=$?
    test "$status" = 1 || exit "$status"
fi
test -n "$name" || exit 1
if branch="$(git --no-optional-locks -c core.fsmonitor=false -C "$base" symbolic-ref --quiet --short HEAD 2>/dev/null)"; then :
else
    status=$?
    test "$status" = 1 || exit "$status"
    branch=""
fi
if ! git --no-optional-locks -c core.fsmonitor=false -C "$base" rev-parse --verify --quiet HEAD >/dev/null 2>&1; then
    printf "available\0%s\0available\0%s\0unavailable\0\0\0\0\0unborn\0" "$name" "$branch"
    exit 0
fi
receipt="$(mktemp)" || exit 1
trap 'rm -f "$receipt"' EXIT HUP INT TERM
if git --no-optional-locks -c core.fsmonitor=false -C "$base" config --get-regexp '^filter\.' > "$receipt" 2>/dev/null; then
    if awk 'tolower($1) ~ /\.(clean|process)$/ { found=1 } END { exit !found }' "$receipt"; then
        printf "available\0%s\0available\0%s\0unavailable\0\0\0\0\0configured_filter\0" "$name" "$branch"
        exit 0
    fi
else
    status=$?
    test "$status" = 1 || exit "$status"
fi
git --no-optional-locks -c core.fsmonitor=false -c diff.external= -C "$base" diff --no-ext-diff --no-textconv --numstat -z HEAD -- > "$receipt" || exit 1
stats="$(awk -v RS="\0" '
    skip { skip -= 1; next }
    {
        first = index($0, "\t")
        rest = substr($0, first + 1)
        second = index(rest, "\t")
        if (!first || !second) { invalid = 1; next }
        added_value = substr($0, 1, first - 1)
        deleted_value = substr(rest, 1, second - 1)
        path = substr(rest, second + 1)
        if (added_value == "-" || deleted_value == "-") binary += 1
        else if (added_value ~ /^[0-9]+$/ && deleted_value ~ /^[0-9]+$/) {
            added += added_value
            deleted += deleted_value
        } else invalid = 1
        if (path == "") skip = 2
    }
    END {
        if (invalid || skip) exit 1
        printf "%d %d %d", added, deleted, binary
    }' "$receipt")" || exit 1
set -- $stats
test "$#" = 3 || exit 1
git --no-optional-locks -c core.fsmonitor=false -C "$base" ls-files --others --exclude-standard -z > "$receipt" || exit 1
untracked="$(awk -v RS="\0" 'END { print NR }' "$receipt")" || exit 1
printf "available\0%s\0available\0%s\0available\0%s\0%s\0%s\0%s\0\0" \
    "$name" "$branch" "$1" "$2" "$3" "$untracked"'''
        result = await self.docker(
            "exec",
            self.name(agent_id),
            "timeout",
            "20",
            "sh",
            "-c",
            script,
            "workspace-context",
            directory,
        )
        return _workspace_context(result)

    @asynccontextmanager
    async def workspace_download(
        self, organization_id: str, agent_id: str, directory: str, path: str
    ) -> AsyncIterator[tuple[dict[str, int | str], AsyncIterator[bytes]]]:
        """Stream a regular workspace file after validating its opened descriptor."""
        if path.startswith("/") or any(part in {"", ".", ".."} for part in path.split("/")):
            raise ValueError("Artifact path must be relative to its mapped workspace")
        await self.inspect(organization_id, agent_id)
        script = (
            'base="$(realpath -- "$1")" || exit 1; test "$base" = "$1" || exit 1; test -f "$base/$2" || exit 1; exec 3< "$base/$2" || exit 1; '
            'opened="$(realpath -- /proc/self/fd/3)" || exit 1; '
            'case "$opened" in "$base"/*) ;; *) exit 1;; esac; '
            "[ -f /proc/self/fd/3 ] || exit 1; "
            'printf "file\\0%s\\0%s\\0" "$(stat -Lc %s /proc/self/fd/3)" '
            '"$(stat -Lc %Y /proc/self/fd/3)"; cat /proc/self/fd/3'
        )
        try:
            process = await asyncio.create_subprocess_exec(
                "docker",
                "exec",
                self.name(agent_id),
                "timeout",
                "120",
                "sh",
                "-c",
                script,
                "workspace-download",
                directory,
                path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            raise RuntimeUnavailable(
                "Docker operation unavailable; check host operations"
            ) from None
        stdout = process.stdout
        assert stdout is not None

        async def close_process() -> None:
            if process.returncode is not None:
                return
            try:
                process.terminate()
            except ProcessLookupError:
                return
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except TimeoutError:
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        return
                await process.wait()

        try:
            fields = [
                (await asyncio.wait_for(stdout.readuntil(b"\0"), timeout=2)).removesuffix(b"\0")
                for _ in range(3)
            ]
            if fields[0] != b"file":
                raise ValueError
            metadata = {"type": "file", "size": int(fields[1]), "modifiedAt": int(fields[2]) * 1000}
        except asyncio.CancelledError:
            await close_process()
            raise
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError, ValueError):
            await close_process()
            raise RuntimeUnavailable("Artifact path is unavailable") from None

        async def chunks() -> AsyncIterator[bytes]:
            while chunk := await stdout.read(64 * 1024):
                yield chunk
            if await process.wait() != 0:
                raise RuntimeUnavailable("Artifact download failed")

        try:
            yield metadata, chunks()
        finally:
            await close_process()

    async def workspace_metadata_many(
        self, organization_id: str, agent_id: str, directory: str, paths: list[str]
    ) -> dict[str, dict[str, int | str]]:
        """Read native-listing metadata in one bounded, descriptor-safe operation."""
        if len(paths) > 512 or any(
            path.startswith("/") or any(part in {"", ".", ".."} for part in path.split("/"))
            for path in paths
        ):
            raise ValueError("Artifact path must be relative to its mapped workspace")
        if not paths:
            return {}
        await self.inspect(organization_id, agent_id)
        script = (
            'base="$(realpath -- "$1")" || exit 1; test "$base" = "$1" || exit 1; shift; '
            'for path do test -f "$base/$path" || test -d "$base/$path" || continue; '
            'exec 3< "$base/$path" || continue; opened="$(realpath -- /proc/self/fd/3)" || continue; '
            'case "$opened" in "$base"/*) ;; *) exec 3<&-; continue;; esac; '
            "if [ -d /proc/self/fd/3 ]; then kind=directory; elif [ -f /proc/self/fd/3 ]; then kind=file; "
            'else exec 3<&-; continue; fi; printf "%s\\0%s\\0%s\\0%s\\0" "$path" "$kind" '
            '"$(stat -Lc %s /proc/self/fd/3)" "$(stat -Lc %Y /proc/self/fd/3)"; exec 3<&-; done'
        )
        try:
            result = await self.docker(
                "exec",
                self.name(agent_id),
                "timeout",
                "20",
                "sh",
                "-c",
                script,
                "workspace-metadata",
                directory,
                *paths,
            )
            fields = result.split(b"\0")
            if fields.pop() != b"" or len(fields) % 4:
                return {}
            allowed = set(paths)
            entries: dict[str, dict[str, int | str]] = {}
            for raw_path, kind, size, modified_at in zip(*[iter(fields)] * 4, strict=True):
                path = raw_path.decode()
                if path not in allowed or kind not in {b"file", b"directory"}:
                    continue
                entries[path] = {
                    "type": kind.decode(),
                    "size": int(size),
                    "modifiedAt": int(modified_at) * 1000,
                }
            return entries
        except (ValueError, UnicodeDecodeError):
            return {}

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
        agent = self.store.agent(organization_id, agent_id)
        configured = agent["applied_envelope"] or agent["desired_envelope"]
        if agent.get("switch_state") == "frozen":
            # The admission gate stays closed until target configuration applies.
            # Every old thread was verified quiet before the atomic freeze; the
            # replacement container may now speak a different native protocol.
            return
        envelope = HostAgentConfiguration.model_validate_json(configured)
        if envelope.configuration.runtime_type == "codex":
            roots = {
                session["session_id"]
                for session in self.store.sessions(organization_id, agent_id)
                if session["runtime_type"] == "codex"
                and session["deleted_at"] is None
                and session.get("frozen_at") is None
            }
            if roots:
                threads = await self.codex_thread_statuses(organization_id, agent_id)
                _assert_codex_threads_quiet(roots, threads)
            return
        directories = {
            session["directory"]
            for session in self.store.sessions(organization_id, agent_id)
            if session.get("frozen_at") is None and session["runtime_type"] == "opencode"
        }
        for directory in [None, *sorted(directories)]:
            statuses = await self.request(
                organization_id, agent_id, "/session/status", directory=directory
            )
            if any(status.get("type") != "idle" for status in statuses.values()):
                raise RuntimeUnavailable("Configuration pending: native work is active")

    async def codex_thread_statuses(
        self, organization_id: str, agent_id: str
    ) -> list[Mapping[str, Any]]:
        """Read complete native ancestry, including mapped unmaterialized roots."""
        source_kinds = [
            "cli",
            "vscode",
            "exec",
            "appServer",
            "subAgent",
            "subAgentReview",
            "subAgentCompact",
            "subAgentThreadSpawn",
            "subAgentOther",
            "unknown",
        ]
        cursor: str | None = None
        seen_cursors: set[str] = set()
        threads: list[Mapping[str, Any]] = []
        while True:
            params: dict[str, object] = {"limit": 100, "sourceKinds": source_kinds}
            if cursor is not None:
                params["cursor"] = cursor
            receipt = await self.codex.call(organization_id, agent_id, "thread/list", params)
            data = receipt.get("data")
            if not isinstance(data, list) or not all(
                isinstance(thread, Mapping) for thread in data
            ):
                raise RuntimeUnavailable("Codex thread status receipt is invalid")
            threads.extend(data)
            next_cursor = receipt.get("nextCursor")
            if next_cursor is None:
                break
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
                raise RuntimeUnavailable("Codex thread status pagination is invalid")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        roots = {
            session["session_id"]
            for session in self.store.sessions(organization_id, agent_id)
            if session["runtime_type"] == "codex"
            and session["deleted_at"] is None
            and session.get("frozen_at") is None
        }
        listed = {thread.get("id") for thread in threads}
        for thread_id in roots - listed:
            receipt = await self.codex.call(
                organization_id, agent_id, "thread/read", {"threadId": thread_id}
            )
            thread = receipt.get("thread")
            if not isinstance(thread, Mapping) or thread.get("id") != thread_id:
                raise RuntimeUnavailable("Codex mapped thread is missing from native status")
            threads.append(thread)
        codex_thread_family(roots, threads)
        return threads

    async def assert_codex_thread_quiet(
        self, organization_id: str, agent_id: str, thread_id: str
    ) -> None:
        """Require the mapped thread and native descendants to have settled."""
        session = self.store.session(organization_id, agent_id, thread_id)
        if session["runtime_type"] != "codex":
            raise RuntimeUnavailable("Thread is not bound to Codex")
        threads = await self.codex_thread_statuses(organization_id, agent_id)
        _assert_codex_threads_quiet({thread_id}, threads)

    async def configure(self, envelope: HostAgentConfiguration) -> None:
        if envelope.configuration.runtime_type == "codex":
            await self.codex.configure(envelope)
            return
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
        directories = {
            session["directory"]
            for session in self.store.sessions(org, agent_id)
            if session.get("frozen_at") is None and session["runtime_type"] == "opencode"
        }
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
            agent = self.store.agent(organization_id, agent_id)
            if agent["desired_state"] != "running" or agent["lifecycle_state"] != "running":
                raise RuntimeUnavailable(
                    "Container replacement is unavailable during a lifecycle transition or stop"
                )
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
        async with self.lock(agent_id):
            self.store.require_writable(organization_id, agent_id)
            agent = self.store.agent(organization_id, agent_id)
            if not agent["applied_envelope"]:
                raise RuntimeUnavailable("Agent configuration is not applied")
            envelope = HostAgentConfiguration.model_validate_json(agent["applied_envelope"])
            if envelope.configuration.runtime_type == "codex":
                return await self.codex.create_session(
                    organization_id,
                    agent_id,
                    title,
                    workspace,
                    directory=directory,
                    metadata=metadata,
                )
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
