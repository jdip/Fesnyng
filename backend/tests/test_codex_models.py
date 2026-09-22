import asyncio
import json
from typing import cast

import pytest

from fesnyng_backend.codex_models import CodexModelDiscovery, _cached_model_slugs
from fesnyng_backend.host_runtime import RuntimeUnavailable


class _Reader:
    def __init__(self):
        self.lines: asyncio.Queue[bytes] = asyncio.Queue()

    async def readline(self):
        return await self.lines.get()

    async def read(self):
        return b""


class _Writer:
    def __init__(self, reader):
        self.reader = reader
        self.sent: list[dict[str, object]] = []

    def write(self, data):
        message = json.loads(data)
        self.sent.append(message)
        request_id = message.get("id")
        if not isinstance(request_id, int):
            return
        method = message["method"]
        if method == "model/list" and message["params"].get("cursor") is None:
            result = {
                "data": [
                    {
                        "id": "gpt-6-astra",
                        "model": "gpt-6-astra",
                        "displayName": "GPT-6 Astra",
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": "low", "description": "Fast"}
                        ],
                        "defaultReasoningEffort": "low",
                    }
                ],
                "nextCursor": "next",
            }
        elif method == "model/list":
            result = {
                "data": [
                    {
                        "id": "gpt-6-sol",
                        "model": "gpt-6-sol",
                        "displayName": "GPT-6 Sol",
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": "medium", "description": "Balanced"}
                        ],
                        "defaultReasoningEffort": "medium",
                    }
                ],
                "nextCursor": None,
            }
        else:
            result = {}
        self.reader.lines.put_nowait(
            json.dumps({"id": request_id, "result": result}).encode() + b"\n"
        )

    async def drain(self):
        return None

    def close(self):
        return None


class _Process:
    def __init__(self):
        self.stdout = _Reader()
        self.stdin = _Writer(self.stdout)
        self.returncode = None
        self.terminated = False

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.terminate()

    async def wait(self):
        self.returncode = 0
        return 0


class _CacheProcess:
    def __init__(self):
        self.stdin = None
        self.returncode = 0

        class Reader:
            async def readline(self):
                return b""

            async def read(self):
                return (
                    b'{"fetched_at":1,"client_version":"0.154.0","models":[{"slug":"gpt-6-astra"}]}'
                )

        self.stdout = Reader()

    def terminate(self):
        return None

    def kill(self):
        return None

    async def wait(self):
        return 0


def test_profile_model_discovery_uses_ephemeral_native_process_and_all_pages(monkeypatch):
    process = _Process()
    commands: list[tuple[str, ...]] = []
    credential_calls: list[tuple[object, ...]] = []

    async def spawn(*args, **_kwargs):
        commands.append(args)
        if args[1] == "run":
            return process
        if args[1] == "exec":
            return _CacheProcess()
        return _Process()

    async def credential(*args, **kwargs):
        credential_calls.append((*args, kwargs))
        return {"access": "private-access", "account_id": "account", "generation": 4}

    monkeypatch.setattr("fesnyng_backend.codex_models.asyncio.create_subprocess_exec", spawn)
    result = asyncio.run(
        CodexModelDiscovery("fesnyng-agent:pinned", credential).list_models("org", "profile")
    )

    models = cast(list[dict[str, object]], result["data"])
    assert [entry["model"] for entry in models] == ["gpt-6-astra"]
    run = commands[0]
    assert run[:10] == (
        "docker",
        "run",
        "--rm",
        "--name",
        run[4],
        "-i",
        "--tmpfs",
        "/home/agent",
        "--entrypoint",
        "/opt/fesnyng/node_modules/.bin/codex",
    )
    assert "private-access" not in run and "account" not in run
    assert commands[1] == (
        "docker",
        "exec",
        run[4],
        "cat",
        "/home/agent/.codex/models_cache.json",
    )
    assert commands[2] == ("docker", "rm", "-f", run[4])
    assert credential_calls == [
        ("org", "profile", {}),
        ("org", "profile", {"previous_account_id": "account"}),
    ]
    assert process.terminated


def test_missing_remote_model_cache_rejects_the_bundled_catalog(monkeypatch):
    class MissingCache:
        stdin = None
        stdout = _CacheProcess().stdout
        returncode = 1

        def terminate(self):
            return None

        def kill(self):
            return None

        async def wait(self):
            return 1

    async def spawn(*_args, **_kwargs):
        return MissingCache()

    monkeypatch.setattr("fesnyng_backend.codex_models.asyncio.create_subprocess_exec", spawn)

    with pytest.raises(RuntimeUnavailable, match="remote model catalog"):
        asyncio.run(_cached_model_slugs("fesnyng-models-proof"))
