"""Verify delegated native effects before treating a parent delivery as settled."""

from typing import Any, Protocol

from pydantic import TypeAdapter, ValidationError

from fesnyng_backend.host_effects import native_tools_running, native_tools_settled
from fesnyng_backend.host_models import NativeID
from fesnyng_backend.host_runtime import RuntimeUnavailable


class NativeRuntime(Protocol):
    async def request(
        self, organization_id: str, agent_id: str, path: str, *, directory: str | None = None
    ) -> Any: ...


class NativeEvidence:
    def __init__(self, runtime: NativeRuntime):
        self.runtime = runtime

    async def delegated_tools_settled(
        self,
        organization_id: str,
        agent_id: str,
        session_id: str,
        directory: str,
        replies: list[dict[str, Any]],
    ) -> bool:
        try:
            return await self._descendants(
                organization_id, agent_id, session_id, directory, replies, {session_id}
            )
        except (RuntimeUnavailable, ValidationError):
            return False

    async def delegated_tools_quiet(
        self,
        organization_id: str,
        agent_id: str,
        session_id: str,
        directory: str,
        replies: list[dict[str, Any]],
    ) -> bool:
        """Allow inspected uncertainty, but never resolve while a descendant still runs."""
        try:
            return await self._descendants(
                organization_id,
                agent_id,
                session_id,
                directory,
                replies,
                {session_id},
                require_settled=False,
            )
        except (RuntimeUnavailable, ValidationError):
            return False

    async def _descendants(
        self,
        org: str,
        agent: str,
        parent: str,
        directory: str,
        messages: list[dict[str, Any]],
        visited: set[str],
        *,
        require_settled: bool = True,
    ) -> bool:
        children: set[str] = set()
        for message in messages:
            for part in message.get("parts", []):
                if part.get("type") != "tool" or part.get("tool") != "task":
                    continue
                state = part.get("state", {})
                if not isinstance(state, dict):
                    return False
                metadata = state.get("metadata", {})
                if not isinstance(metadata, dict):
                    return False
                child = metadata.get("sessionId")
                if child is None:
                    # A denied task may never have created a child. A completed task must have one.
                    if state.get("status") == "completed":
                        return False
                    continue
                children.add(TypeAdapter(NativeID).validate_python(child))
        for child in children:
            if child in visited:
                return False
            visited.add(child)
            info = await self.runtime.request(org, agent, f"/session/{child}", directory=directory)
            if (
                not isinstance(info, dict)
                or info.get("id") != child
                or info.get("parentID") != parent
                or info.get("directory") != directory
            ):
                return False
            statuses = await self.runtime.request(
                org, agent, "/session/status", directory=directory
            )
            if not isinstance(statuses, dict):
                return False
            status = statuses.get(child, {})
            if not isinstance(status, dict) or status.get("type", "idle") != "idle":
                return False
            history = await self.runtime.request(
                org, agent, f"/session/{child}/message", directory=directory
            )
            if (
                not isinstance(history, list)
                or not history
                or not all(
                    isinstance(item, dict)
                    and isinstance(item.get("info"), dict)
                    and item["info"].get("sessionID") == child
                    and isinstance(item.get("parts"), list)
                    and all(isinstance(part, dict) for part in item["parts"])
                    for item in history
                )
            ):
                return False
            assistants = [
                item["info"] for item in history if item["info"].get("role") == "assistant"
            ]
            if require_settled:
                if not assistants or not assistants[-1].get("time", {}).get("completed"):
                    return False
                if not native_tools_settled(history):
                    return False
            elif native_tools_running(history):
                return False
            if not await self._descendants(
                org, agent, child, directory, history, visited, require_settled=require_settled
            ):
                return False
        return True
