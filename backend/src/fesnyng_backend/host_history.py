"""Capture verified native history before the host commits a permanent freeze."""

from collections.abc import Mapping
from typing import Any

from fesnyng_backend.codex_history import full_turns
from fesnyng_backend.host_effects import native_tools_running
from fesnyng_backend.host_runtime import RuntimeUnavailable, codex_thread_family


async def capture_history(host: Any, runtime: Any, org: str, agent: str) -> dict[str, Any]:
    """Caller holds the admission gate and runtime lock throughout capture/commit."""
    roots = [row for row in host.sessions(org, agent, archived=None) if not row.get("frozen_at")]
    snapshots = {}
    for root in roots:
        if root["runtime_type"] == "codex":
            snapshot = await _codex(runtime, org, agent, root)
        else:
            snapshot = await _opencode(runtime, org, agent, root, {r["session_id"] for r in roots})
        snapshots[root["session_id"]] = snapshot
    return snapshots


async def capture_session_history(
    host: Any, runtime: Any, org: str, agent: str, root: dict[str, Any]
) -> dict[str, Any]:
    """Capture one exact root before a workspace lifecycle operation changes files."""
    if root["runtime_type"] == "codex":
        return await _codex(runtime, org, agent, root)
    mapped = {row["session_id"] for row in host.sessions(org, agent, archived=None)}
    return await _opencode(runtime, org, agent, root, mapped)


async def _opencode(runtime: Any, org: str, agent: str, root: dict, mapped: set[str]) -> dict:
    children: dict[str, Any] = {}
    seen = {root["session_id"]}

    async def capture(session_id: str, directory: str) -> dict:
        metadata = await runtime.request(org, agent, f"/session/{session_id}", directory=directory)
        if (
            not isinstance(metadata, dict)
            or metadata.get("id") != session_id
            or metadata.get("directory") != directory
        ):
            raise RuntimeUnavailable("Native history metadata is invalid")
        history = await runtime.request(
            org, agent, f"/session/{session_id}/message", directory=directory
        )
        if not isinstance(history, list) or not all(
            isinstance(item, dict)
            and isinstance(item.get("info"), dict)
            and item["info"].get("sessionID") == session_id
            and isinstance(item.get("parts"), list)
            and all(isinstance(part, dict) for part in item["parts"])
            for item in history
        ):
            raise RuntimeUnavailable("Native history receipt is invalid")
        if native_tools_running(history):
            raise RuntimeUnavailable("Native history contains active tool effects")
        descendants = await runtime.request(
            org, agent, f"/session/{session_id}/children", directory=directory
        )
        if not isinstance(descendants, list):
            raise RuntimeUnavailable("Native history child receipt is invalid")
        for child in descendants:
            if (
                not isinstance(child, Mapping)
                or not isinstance(child.get("id"), str)
                or child.get("parentID") != session_id
                or not isinstance(child.get("directory"), str)
            ):
                raise RuntimeUnavailable("Native history child ancestry is invalid")
            child_id = child["id"]
            if child_id in mapped:
                continue
            if child_id in seen:
                raise RuntimeUnavailable("Native history child ancestry is cyclic")
            seen.add(child_id)
            children[child_id] = await capture(child_id, child["directory"])
        return {"session": metadata, "history": history}

    snapshot = await capture(root["session_id"], root["directory"])
    return {**snapshot, "runtime_type": "opencode", "children": children}


async def _codex(runtime: Any, org: str, agent: str, root: dict) -> dict:
    adapter = runtime.codex

    async def capture(thread_id: str, parent: str | None = None) -> dict:
        result = await adapter.call(org, agent, "thread/read", {"threadId": thread_id})
        thread = result.get("thread") if isinstance(result, Mapping) else None
        if not isinstance(thread, dict) or thread.get("id") != thread_id:
            raise RuntimeUnavailable("Codex history metadata is invalid")
        if (
            not isinstance(thread.get("cwd"), str)
            or (thread_id == root["session_id"] and thread["cwd"] != root["directory"])
            or (parent is not None and thread.get("parentThreadId") != parent)
        ):
            raise RuntimeUnavailable("Codex history workspace or ancestry is invalid")
        try:
            turns = await full_turns(adapter, org, agent, thread_id)
        except RuntimeUnavailable as error:
            # Only this thread-specific native response proves that no user turn
            # has ever existed. A generic unsupported/read failure proves nothing.
            if str(error) != (
                f"thread {thread_id} is not materialized yet; "
                "thread/turns/list is unavailable before first user message"
            ):
                raise
            turns = []
        if not all(
            isinstance(turn.get("items"), list)
            and all(isinstance(item, dict) for item in turn["items"])
            and turn.get("status") in {"completed", "interrupted", "failed"}
            for turn in turns
        ):
            raise RuntimeUnavailable("Codex history is incomplete or active")
        tools = {
            "commandExecution",
            "fileChange",
            "mcpToolCall",
            "dynamicToolCall",
            "collabAgentToolCall",
        }
        if any(
            item.get("status") == "inProgress"
            or (
                item.get("type") in tools
                and item.get("status") not in {"completed", "failed", "declined"}
            )
            for turn in turns
            for item in turn["items"]
        ):
            raise RuntimeUnavailable("Codex history contains unsettled native tools")
        return {
            "session": thread,
            "history": {"thread": thread, "turns": turns, "historyState": "complete"},
        }

    snapshot = await capture(root["session_id"])
    family = codex_thread_family(
        {root["session_id"]}, await runtime.codex_thread_statuses(org, agent)
    )
    children = {
        thread["id"]: await capture(thread["id"], thread.get("parentThreadId"))
        for thread in family
        if thread["id"] != root["session_id"]
    }
    return {**snapshot, "runtime_type": "codex", "children": children}
