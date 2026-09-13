import asyncio
from typing import Any

from fesnyng_backend.host_native_evidence import NativeEvidence


def task_part(child):
    return {
        "info": {"id": "msg_parent", "role": "assistant"},
        "parts": [
            {
                "type": "tool",
                "tool": "task",
                "state": {"status": "completed", "metadata": {"sessionId": child}},
            }
        ],
    }


class Native:
    def __init__(self):
        self.parent = "ses_parent"
        self.busy = False
        self.history: list[dict[str, Any]] = [
            {
                "info": {
                    "id": "msg_child",
                    "role": "assistant",
                    "sessionID": "ses_child",
                    "time": {"completed": 1},
                    "finish": "stop",
                },
                "parts": [
                    {
                        "type": "tool",
                        "tool": "bash",
                        "state": {"status": "completed", "metadata": {"exit": 0}},
                    }
                ],
            }
        ]

    async def request(self, organization_id, agent_id, path, *, directory=None):
        assert directory == "/workspace/thread"
        if path == "/session/ses_child":
            return {"id": "ses_child", "parentID": self.parent, "directory": directory}
        if path == "/session/status":
            return {"ses_child": {"type": "busy"}} if self.busy else {}
        if path == "/session/ses_child/message":
            return self.history
        raise AssertionError(path)


def test_parent_task_receipt_requires_settled_child_effects():
    native = Native()
    evidence = NativeEvidence(native)

    async def check():
        assert await evidence.delegated_tools_settled(
            "org", "agent", "ses_parent", "/workspace/thread", [task_part("ses_child")]
        )
        native.history[0]["parts"][0]["state"]["metadata"]["exit"] = None
        assert not await evidence.delegated_tools_settled(
            "org", "agent", "ses_parent", "/workspace/thread", [task_part("ses_child")]
        )
        native.history[0]["parts"][0]["state"] = {
            "status": "error",
            "error": "Tool execution aborted",
            "metadata": {"interrupted": True},
        }
        assert not await evidence.delegated_tools_settled(
            "org", "agent", "ses_parent", "/workspace/thread", [task_part("ses_child")]
        )

    asyncio.run(check())


def test_child_evidence_requires_ancestry_idle_and_readable_history():
    native = Native()
    evidence = NativeEvidence(native)

    async def settled():
        return await evidence.delegated_tools_settled(
            "org", "agent", "ses_parent", "/workspace/thread", [task_part("ses_child")]
        )

    native.parent = "ses_other"
    assert not asyncio.run(settled())
    native.parent = "ses_parent"
    native.busy = True
    assert not asyncio.run(settled())
    native.busy = False
    native.history = []
    assert not asyncio.run(settled())


def test_explicit_resolution_requires_quiet_children_but_allows_inspected_interruption():
    native = Native()
    evidence = NativeEvidence(native)

    async def quiet():
        return await evidence.delegated_tools_quiet(
            "org", "agent", "ses_parent", "/workspace/thread", [task_part("ses_child")]
        )

    native.busy = True
    assert not asyncio.run(quiet())
    native.busy = False
    native.history[0]["parts"][0]["state"] = {"status": "running"}
    assert not asyncio.run(quiet())
    native.history[0]["parts"][0]["state"] = {"status": "error", "metadata": {"interrupted": True}}
    assert asyncio.run(quiet())
