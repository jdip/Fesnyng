"""Validated full-turn pagination for the pinned Codex App Server."""

from collections.abc import Mapping
from typing import Any

from fesnyng_backend.host_runtime import RuntimeUnavailable


def is_unmaterialized(error: RuntimeUnavailable, thread_id: str | None = None) -> bool:
    """Recognize only the pinned server's known-empty-thread response."""
    message = str(error)
    if message == "list_turns is not supported yet":
        return True
    return thread_id is not None and message == (
        f"thread {thread_id} is not materialized yet; "
        "thread/turns/list is unavailable before first user message"
    )


async def full_turns(
    adapter: Any, organization_id: str, agent_id: str, thread_id: str
) -> list[Any]:
    """Read every full native turn page, rejecting cursor loops or malformed cursors."""
    turns: list[Any] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    seen_turns: set[str] = set()
    for _ in range(100):
        params: dict[str, object] = {
            "threadId": thread_id,
            "itemsView": "full",
            "sortDirection": "asc",
        }
        if cursor is not None:
            params["cursor"] = cursor
        page = await adapter.call(organization_id, agent_id, "thread/turns/list", params)
        data = page.get("data") if isinstance(page, Mapping) else None
        next_cursor = page.get("nextCursor") if isinstance(page, Mapping) else None
        backwards_cursor = page.get("backwardsCursor") if isinstance(page, Mapping) else None
        if (
            not isinstance(data, list)
            or not all(
                isinstance(turn, Mapping) and isinstance(turn.get("id"), str) for turn in data
            )
            or (next_cursor is not None and not isinstance(next_cursor, str))
            or (backwards_cursor is not None and not isinstance(backwards_cursor, str))
        ):
            raise RuntimeUnavailable("Codex turn history receipt is invalid")
        for turn in data:
            turn_id = turn["id"]
            if turn_id in seen_turns:
                raise RuntimeUnavailable("Codex turn history pagination is invalid")
            seen_turns.add(turn_id)
            turns.append(dict(turn))
        if next_cursor is None:
            return turns
        if not next_cursor or next_cursor in seen_cursors:
            raise RuntimeUnavailable("Codex turn history pagination is invalid")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    raise RuntimeUnavailable("Codex turn history pagination exceeded its safe bound")
