import asyncio
from typing import Any, cast
from uuid import uuid4

import pytest

from fesnyng_backend.host_history import capture_history
from fesnyng_backend.host_models import Actor
from fesnyng_backend.host_runtime import RuntimeUnavailable
from fesnyng_backend.host_workspace import Fork, Revert, SessionUpdate, Workspace


class Host:
    def sessions(self, org, agent, *, archived=None):
        return [
            {
                "session_id": "root",
                "directory": "/workspace/proof/root",
                "runtime_type": "opencode",
                "title": "Retained",
                "frozen_at": None,
            }
        ]


class Native:
    def __init__(self):
        self.invalid = False

    async def request(self, org, agent, path, *, directory=None):
        session = path.split("/")[2]
        if path.endswith("/children"):
            return (
                [{"id": "child", "parentID": "root", "directory": directory}]
                if session == "root"
                else []
            )
        if path.endswith("/message"):
            return [
                {
                    "info": {"id": "msg", "sessionID": "wrong" if self.invalid else session},
                    "parts": [
                        {
                            "type": "tool",
                            "tool": "apply_patch",
                            "state": {
                                "status": "completed",
                                "output": "file added",
                                "metadata": {"diff": "+retained"},
                            },
                        }
                    ],
                }
            ]
        return {"id": session, "directory": directory, "title": "Retained"}


def test_capture_preserves_native_tool_output_diffs_and_child_history():
    native = Native()
    snapshot = asyncio.run(capture_history(Host(), native, "org", "agent"))["root"]
    assert snapshot["runtime_type"] == "opencode"
    assert snapshot["history"][0]["parts"][0]["state"]["metadata"] == {"diff": "+retained"}
    assert snapshot["children"]["child"]["history"][0]["info"]["sessionID"] == "child"
    native.invalid = True
    with pytest.raises(RuntimeUnavailable, match="history"):
        asyncio.run(capture_history(Host(), native, "org", "agent"))


class FrozenHost(Host):
    def __init__(self):
        self.snapshot = asyncio.run(capture_history(Host(), Native(), "org", "agent"))["root"]

    def sessions(self, org, agent, *, archived=None):
        return [
            {**row, "frozen_at": 123, "archived_at": None, "created_at": 1}
            for row in super().sessions(org, agent)
        ]

    def session(self, org, agent, session_id):
        if session_id != "root":
            raise LookupError("Thread not found")
        return self.sessions(org, agent)[0]

    def frozen_snapshot(self, org, agent, session_id):
        return self.snapshot if session_id == "root" else None

    def require_writable(self, org, agent, session_id=None):
        raise RuntimeUnavailable("Thread is permanently frozen and read-only")


class StoppedNative:
    def lock(self, agent_id):
        return asyncio.Lock()

    async def request(self, *args, **kwargs):
        raise AssertionError("Frozen reads must not contact native harness")


def test_frozen_root_and_child_history_remain_readable_without_native_harness():
    workspace = Workspace(
        cast(Any, FrozenHost()), cast(Any, StoppedNative()), cast(Any, None), cast(Any, None)
    )

    async def read():
        root = await workspace.get("org", "agent", "root")
        assert root["frozen"] is True
        assert root["runtime_type"] == "opencode"
        child = await workspace.messages("org", "agent", "child")
        assert child[0]["parts"][0]["state"]["output"] == "file added"

    asyncio.run(read())


def test_frozen_direct_native_actions_are_rejected_before_contacting_harness():
    workspace = Workspace(
        cast(Any, FrozenHost()), cast(Any, StoppedNative()), cast(Any, None), cast(Any, None)
    )

    async def mutate():
        for action in (
            lambda: workspace.update("org", "agent", "root", SessionUpdate(title="Changed")),
            lambda: workspace.delete("org", "agent", "root"),
            lambda: workspace.revert("org", "agent", "root", Revert(messageID="msg")),
            lambda: workspace.unrevert("org", "agent", "root"),
            lambda: workspace.fork(
                "org", "agent", "root", Fork(), Actor(kind="human", id=uuid4(), name="Owner")
            ),
        ):
            with pytest.raises(RuntimeUnavailable, match="permanently frozen"):
                await action()

    asyncio.run(mutate())


class CodexHost(Host):
    def sessions(self, org, agent, *, archived=None):
        return [{**row, "runtime_type": "codex"} for row in super().sessions(org, agent)]


class CodexNative:
    def __init__(self):
        self.codex = self
        self.bad_page = False

    async def codex_thread_statuses(self, org, agent):
        return [{"id": "root"}, {"id": "child", "parentThreadId": "root"}]

    async def call(self, org, agent, method, params):
        if method == "thread/read":
            return {
                "thread": {
                    "id": params["threadId"],
                    "title": "Native",
                    "turns": [],
                    "cwd": "/workspace/proof/root",
                    "parentThreadId": "root" if params["threadId"] == "child" else None,
                }
            }
        assert method == "thread/turns/list"
        if not params.get("cursor"):
            return {
                "data": [
                    {
                        "id": "first",
                        "status": "completed",
                        "items": [
                            {
                                "type": "commandExecution",
                                "id": "cmd",
                                "status": "completed",
                                "aggregatedOutput": "retained output",
                            }
                        ],
                    }
                ],
                "nextCursor": "last",
            }
        return {
            "data": [
                {
                    "id": "first" if self.bad_page else "second",
                    "status": "completed",
                    "items": [
                        {
                            "type": "fileChange",
                            "id": "patch",
                            "status": "completed",
                            "changes": [
                                {"path": "proof.txt", "kind": {"type": "add"}, "diff": "+proof"}
                            ],
                        }
                    ],
                }
            ],
            "nextCursor": None,
        }


def test_codex_capture_preserves_every_full_turn_page_and_rejects_repeated_pages():
    native = CodexNative()
    snapshot = asyncio.run(capture_history(CodexHost(), native, "org", "agent"))["root"]
    assert [turn["id"] for turn in snapshot["history"]["turns"]] == ["first", "second"]
    assert snapshot["history"]["turns"][1]["items"][0]["changes"][0]["diff"] == "+proof"
    assert snapshot["children"]["child"]["history"]["historyState"] == "complete"
    native.bad_page = True
    with pytest.raises(RuntimeUnavailable, match="pagination"):
        asyncio.run(capture_history(CodexHost(), native, "org", "agent"))


def test_codex_capture_accepts_only_thread_specific_proof_of_empty_history():
    class Empty(CodexNative):
        generic = False

        async def call(self, org, agent, method, params):
            if method == "thread/turns/list":
                raise RuntimeUnavailable(
                    "list_turns is not supported yet"
                    if self.generic
                    else f"thread {params['threadId']} is not materialized yet; "
                    "thread/turns/list is unavailable before first user message"
                )
            return await super().call(org, agent, method, params)

    native = Empty()
    snapshot = asyncio.run(capture_history(CodexHost(), native, "org", "agent"))["root"]
    assert snapshot["history"]["turns"] == []
    assert snapshot["history"]["historyState"] == "complete"
    native.generic = True
    with pytest.raises(RuntimeUnavailable, match="not supported"):
        asyncio.run(capture_history(CodexHost(), native, "org", "agent"))


@pytest.mark.parametrize("invalid", ["workspace", "tool"])
def test_codex_capture_rejects_workspace_mismatch_and_unsettled_tool(invalid):
    class Invalid(CodexNative):
        async def call(self, org, agent, method, params):
            result = await super().call(org, agent, method, params)
            if invalid == "workspace" and method == "thread/read":
                result["thread"]["cwd"] = "/workspace/other-thread"
            if invalid == "tool" and method == "thread/turns/list":
                result["data"][0]["items"][0]["status"] = "inProgress"
            return result

    with pytest.raises(RuntimeUnavailable, match="history"):
        asyncio.run(capture_history(CodexHost(), Invalid(), "org", "agent"))


def test_native_thread_creation_rechecks_switch_gate_before_any_docker_action(
    tmp_path, monkeypatch
):
    from fesnyng_backend.host_runtime import DockerRuntime
    from test_codex_workspace import _app

    app, org, agent, _token, _native = _app(tmp_path)
    host = app.state.host_store
    host.begin_harness_switch(org, agent, 1, "opencode")
    runtime = DockerRuntime(host, "http://host.invalid")

    async def docker(*args, **kwargs):
        raise AssertionError("Native creation reached Docker during switch")

    monkeypatch.setattr(runtime, "docker", docker)
    with pytest.raises(ValueError, match="switch is in progress"):
        asyncio.run(runtime.create_session(org, agent, "New", "default"))
