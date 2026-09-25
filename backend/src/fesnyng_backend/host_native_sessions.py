"""Validate native child receipts without choosing traversal or mapped-root policy."""

from collections.abc import Iterator, Mapping

from pydantic import TypeAdapter, ValidationError

from fesnyng_backend.host_models import NativeID
from fesnyng_backend.host_runtime import RuntimeUnavailable

_native_id = TypeAdapter(NativeID)


def native_children(
    response: object, parent_id: str, *, require_nonempty_directory: bool
) -> Iterator[tuple[str, str]]:
    """Validate lazily so a scoped lookup can stop at its first verified match."""
    if not isinstance(response, list):
        raise RuntimeUnavailable("Native child sessions response is invalid")
    for child in response:
        if not isinstance(child, Mapping):
            raise RuntimeUnavailable("Native child session receipt is invalid")
        try:
            child_id = _native_id.validate_python(child.get("id"))
        except ValidationError:
            raise RuntimeUnavailable("Native child session receipt is invalid") from None
        directory = child.get("directory")
        if (
            child.get("parentID") != parent_id
            or not isinstance(directory, str)
            or (require_nonempty_directory and not directory)
        ):
            raise RuntimeUnavailable("Native child session ancestry is invalid")
        yield child_id, directory


def visit_native_child(child_id: str, seen: set[str]) -> None:
    """Reject repeated descendants, including cycles, within the caller's tree."""
    if child_id in seen:
        raise RuntimeUnavailable("Native child session ancestry is invalid")
    seen.add(child_id)
