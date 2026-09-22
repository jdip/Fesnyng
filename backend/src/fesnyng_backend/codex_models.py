"""Short-lived, profile-scoped Codex App Server model discovery."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol, cast
from uuid import uuid4

from fesnyng_backend.host_runtime import RuntimeUnavailable

CredentialAccess = Callable[..., Awaitable[dict[str, object]]]


class _Stream(Protocol):
    def write(self, data: bytes) -> None: ...

    async def drain(self) -> None: ...

    def close(self) -> None: ...


class _Reader(Protocol):
    async def readline(self) -> bytes: ...

    async def read(self) -> bytes: ...


class _Process(Protocol):
    @property
    def stdin(self) -> _Stream | None: ...

    @property
    def stdout(self) -> _Reader | None: ...

    @property
    def returncode(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    async def wait(self) -> int: ...


class CodexModelDiscovery:
    """Run no-persistence App Server discovery without touching an agent runtime."""

    def __init__(self, image: str, credential_access: CredentialAccess) -> None:
        self.image = image
        self.credential_access = credential_access

    async def list_models(self, organization_id: str, profile_id: str) -> dict[str, object]:
        name = f"fesnyng-models-{uuid4().hex}"
        process: _Process | None = None
        try:
            credential = await self.credential_access(organization_id, profile_id)
            process = await asyncio.create_subprocess_exec(
                "docker",
                "run",
                "--rm",
                "--name",
                name,
                "-i",
                "--tmpfs",
                "/home/agent",
                "--entrypoint",
                "/opt/fesnyng/node_modules/.bin/codex",
                self.image,
                "app-server",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            assert process is not None
        except (OSError, PermissionError, KeyError, TypeError, ValueError):
            raise RuntimeUnavailable("Codex model discovery is unavailable") from None
        try:
            async with asyncio.timeout(30):
                rpc = _RPC(process, self.credential_access, organization_id, profile_id)
                await rpc.request(
                    "initialize",
                    {
                        "clientInfo": {
                            "name": "fesnyng",
                            "title": "Fesnyng model discovery",
                            "version": "0.1.0",
                        },
                        "capabilities": {"experimentalApi": True},
                    },
                )
                await rpc.notify("initialized", {})
                rpc.credential_generation = _generation(credential)
                await rpc.request(
                    "account/login/start",
                    {
                        "type": "chatgptAuthTokens",
                        "accessToken": credential["access"],
                        "chatgptAccountId": credential["account_id"],
                    },
                )
                inventory = await model_inventory(lambda params: rpc.request("model/list", params))
                slugs = await _cached_model_slugs(name)
                # The profile cannot move accounts during a discovery. Check it
                # again before returning the remote-provenanced catalog.
                await self.credential_access(
                    organization_id,
                    profile_id,
                    previous_account_id=credential["account_id"],
                )
                return _remote_inventory(inventory, slugs)
        except (PermissionError, KeyError, TypeError, ValueError, TimeoutError) as error:
            raise RuntimeUnavailable("Codex model discovery failed") from error
        finally:
            if process is not None:
                await _stop(process)
                await _remove(name)


class _RPC:
    def __init__(
        self,
        process: _Process,
        credential_access: CredentialAccess,
        organization_id: str,
        profile_id: str,
    ) -> None:
        self.process = process
        self.credential_access = credential_access
        self.organization_id = organization_id
        self.profile_id = profile_id
        self.next_id = 1
        self.credential_generation: int | None = None

    async def notify(self, method: str, params: Mapping[str, object]) -> None:
        await self._write({"method": method, "params": dict(params)})

    async def request(self, method: str, params: Mapping[str, object]) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        await self._write({"id": request_id, "method": method, "params": dict(params)})
        while True:
            stdout = self.process.stdout
            if stdout is None:
                raise RuntimeUnavailable("Codex model discovery did not start")
            line = await stdout.readline()
            if not line:
                raise RuntimeUnavailable("Codex model discovery ended unexpectedly")
            message = json.loads(line)
            if not isinstance(message, dict):
                raise RuntimeUnavailable("Codex model discovery returned invalid JSON")
            if "id" in message and isinstance(message.get("method"), str):
                await self._server_request(message)
            elif message.get("id") == request_id:
                result = message.get("result")
                if isinstance(result, dict):
                    return result
                raise RuntimeUnavailable("Codex model discovery request was rejected")

    async def _server_request(self, message: Mapping[str, object]) -> None:
        request_id, method, params = message.get("id"), message.get("method"), message.get("params")
        if method != "account/chatgptAuthTokens/refresh" or not isinstance(params, Mapping):
            await self._write(
                {
                    "id": request_id,
                    "error": {"code": -32601, "message": "Unsupported server request"},
                }
            )
            return
        try:
            credential = await self.credential_access(
                self.organization_id,
                self.profile_id,
                rejected_generation=self.credential_generation,
                previous_account_id=params.get("previousAccountId"),
            )
            self.credential_generation = _generation(credential)
            result = {
                "accessToken": credential["access"],
                "chatgptAccountId": credential["account_id"],
            }
            await self._write({"id": request_id, "result": result})
        except (PermissionError, KeyError, TypeError, ValueError):
            await self._write(
                {"id": request_id, "error": {"code": -32000, "message": "Credential unavailable"}}
            )

    async def _write(self, value: Mapping[str, object]) -> None:
        stdin = self.process.stdin
        if stdin is None:
            raise RuntimeUnavailable("Codex model discovery did not start")
        stdin.write(json.dumps(value, separators=(",", ":")).encode() + b"\n")
        await stdin.drain()


async def model_inventory(
    call: Callable[[Mapping[str, object]], Awaitable[dict[str, Any]]],
) -> dict[str, object]:
    cursor: str | None = None
    seen_cursors: set[str] = set()
    seen_models: set[str] = set()
    models: list[dict[str, object]] = []
    while True:
        # Bound each JSON-RPC line well below asyncio's reader limit while still
        # traversing every opaque native page.
        params: dict[str, object] = {"includeHidden": True, "limit": 20}
        if cursor is not None:
            params["cursor"] = cursor
        receipt = await call(params)
        data = receipt.get("data")
        if not isinstance(data, list):
            raise RuntimeUnavailable("Codex model inventory receipt is invalid")
        for entry in data:
            model = model_record(entry)
            model_id = cast(str, model["model"])
            if model_id in seen_models:
                raise RuntimeUnavailable("Codex model inventory contains duplicate models")
            seen_models.add(model_id)
            models.append(model)
        next_cursor = receipt.get("nextCursor")
        if next_cursor is None:
            return {"data": models, "nextCursor": None}
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
            raise RuntimeUnavailable("Codex model inventory pagination is invalid")
        seen_cursors.add(next_cursor)
        cursor = next_cursor


def model_record(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise RuntimeUnavailable("Codex model inventory receipt is invalid")
    model = value.get("model", value.get("id"))
    display_name = value.get("displayName")
    efforts = value.get("supportedReasoningEfforts", [])
    default_effort = value.get("defaultReasoningEffort")
    if (
        not isinstance(model, str)
        or not model
        or not isinstance(display_name, str)
        or not display_name
        or not isinstance(efforts, list)
        or not isinstance(default_effort, str)
        or not default_effort
    ):
        raise RuntimeUnavailable("Codex model inventory receipt is invalid")
    supported: list[dict[str, str]] = []
    names: set[str] = set()
    for effort in efforts:
        if not isinstance(effort, Mapping):
            raise RuntimeUnavailable("Codex model inventory receipt is invalid")
        name, description = effort.get("reasoningEffort"), effort.get("description")
        if (
            not isinstance(name, str)
            or not name
            or name in names
            or not isinstance(description, str)
        ):
            raise RuntimeUnavailable("Codex model inventory receipt is invalid")
        names.add(name)
        supported.append({"reasoningEffort": name, "description": description})
    if default_effort not in names:
        raise RuntimeUnavailable("Codex model inventory receipt is invalid")
    return {
        "model": model,
        "displayName": display_name,
        "supportedReasoningEfforts": supported,
        "defaultReasoningEffort": default_effort,
    }


async def _cached_model_slugs(name: str) -> set[str]:
    """Require the fresh native remote cache, never App Server's bundled fallback."""
    try:
        process = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            name,
            "cat",
            "/home/agent/.codex/models_cache.json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        raise RuntimeUnavailable("Codex model discovery cache is unavailable") from None
    stdout = process.stdout
    if stdout is None:
        raise RuntimeUnavailable("Codex model discovery cache is unavailable")
    try:
        async with asyncio.timeout(5):
            raw = await stdout.read()
            status = await process.wait()
    except TimeoutError:
        await _stop(process)
        raise RuntimeUnavailable("Codex model discovery cache timed out") from None
    if status != 0:
        raise RuntimeUnavailable("Codex model discovery did not receive a remote model catalog")
    try:
        cache = json.loads(raw)
    except (TypeError, ValueError):
        raise RuntimeUnavailable("Codex model discovery cache is invalid") from None
    if not isinstance(cache, Mapping):
        raise RuntimeUnavailable("Codex model discovery cache is invalid")
    fetched_at, client_version, models = (
        cache.get("fetched_at"),
        cache.get("client_version"),
        cache.get("models"),
    )
    if (
        not isinstance(fetched_at, (int, float, str))
        or isinstance(fetched_at, str)
        and not fetched_at
        or not isinstance(client_version, str)
        or not client_version
        or not isinstance(models, list)
    ):
        raise RuntimeUnavailable("Codex model discovery cache is invalid")
    slugs = {
        entry.get("slug")
        for entry in models
        if isinstance(entry, Mapping) and isinstance(entry.get("slug"), str) and entry["slug"]
    }
    if len(slugs) != len(models):
        raise RuntimeUnavailable("Codex model discovery cache is invalid")
    return slugs


def _remote_inventory(inventory: Mapping[str, object], slugs: set[str]) -> dict[str, object]:
    data = inventory.get("data")
    if not isinstance(data, list):
        raise RuntimeUnavailable("Codex model inventory receipt is invalid")
    return {
        "data": [
            model
            for model in data
            if isinstance(model, Mapping)
            and isinstance(model.get("model"), str)
            and model["model"] in slugs
        ],
        "nextCursor": None,
    }


async def _stop(process: _Process) -> None:
    stdin = process.stdin
    if stdin is not None:
        stdin.close()
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            async with asyncio.timeout(2):
                await process.wait()
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()


async def _remove(name: str) -> None:
    """Remove only the named discovery container if Docker retained it after a timeout."""
    try:
        process = await asyncio.create_subprocess_exec(
            "docker",
            "rm",
            "-f",
            name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        raise RuntimeUnavailable("Codex model discovery cleanup is unavailable") from None
    try:
        async with asyncio.timeout(5):
            await process.wait()
    except TimeoutError:
        await _stop(process)
        raise RuntimeUnavailable("Codex model discovery cleanup timed out") from None
    if process.returncode == 0:
        return
    stderr = process.stderr
    detail = await stderr.read() if stderr is not None else b""
    # --rm normally removes an attached process's container before this exact
    # fallback runs. Docker's documented missing-container response is safe.
    if b"No such container" in detail:
        return
    raise RuntimeUnavailable("Codex model discovery cleanup failed")


def _generation(credential: Mapping[str, object]) -> int:
    generation = credential.get("generation")
    if not isinstance(generation, int) or generation < 0:
        raise ValueError("Credential unavailable")
    return generation
