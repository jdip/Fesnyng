"""Evidence predicates for native tool effects."""

from typing import Any


def native_tools_running(messages: list[dict[str, Any]]) -> bool:
    """Whether native history fails to prove every observed tool is terminal."""
    for message in messages:
        if not isinstance(message, dict):
            continue
        for part in message.get("parts", []):
            if not isinstance(part, dict) or part.get("type") != "tool":
                continue
            state = part.get("state")
            if not isinstance(state, dict) or state.get("status") not in {"completed", "error"}:
                return True
    return False


def native_tools_settled(messages: list[dict[str, Any]]) -> bool:
    """Whether native history proves every observed tool effect reached a safe outcome."""
    for message in messages:
        if not isinstance(message, dict):
            continue
        for part in message.get("parts", []):
            if not isinstance(part, dict) or part.get("type") != "tool":
                continue
            state = part.get("state")
            if not isinstance(state, dict):
                return False
            if state.get("status") not in {"completed", "error"}:
                return False
            metadata = state.get("metadata")
            if isinstance(metadata, dict) and metadata.get("interrupted") is True:
                return False
            if state.get("error") == "Tool execution aborted":
                return False
            if (
                state.get("status") == "completed"
                and part.get("tool") in {"bash", "shell"}
                and (not isinstance(metadata, dict) or metadata.get("exit") is None)
            ):
                return False
    return True
