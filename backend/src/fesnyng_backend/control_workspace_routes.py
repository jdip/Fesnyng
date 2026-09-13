"""Human-authorized, allowlisted OpenCode facade through the assigned agent host."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from fesnyng_backend import auth
from fesnyng_backend.control_host_routes import host_client, host_errors
from fesnyng_backend.host_client import HostResponse, HostStream
from fesnyng_backend.host_models import Actor

router = APIRouter(
    prefix="/organizations/{organization_id}/agents/{agent_id}/opencode", tags=["workspace"]
)

_NATIVE_ID = r"[A-Za-z0-9_-]{1,160}"
_SESSION = re.compile(rf"^session/{_NATIVE_ID}$")
_SESSION_READ = re.compile(rf"^session/{_NATIVE_ID}/(?:message|status)$")
_SESSION_MUTATION = re.compile(
    rf"^session/{_NATIVE_ID}/(?:prompt_async|abort|revert|unrevert|fork)$"
)
_INTERACTION_REPLY = re.compile(rf"^(?:question|permission)/{_NATIVE_ID}/(?:reply|reject)$")
_ARTIFACT_PATHS = {"file", "file/content"}
_READ_PATHS = {
    "experimental/session",
    "session",
    "session/status",
    "question",
    "permission",
    "provider",
    "config",
    "command",
    "event",
    *_ARTIFACT_PATHS,
}


def _allowed(method: str, path: str) -> bool:
    if method == "GET":
        return bool(
            path in _READ_PATHS or _SESSION.fullmatch(path) or _SESSION_READ.fullmatch(path)
        )
    if method == "POST":
        return bool(
            path == "session"
            or _SESSION_MUTATION.fullmatch(path)
            or _INTERACTION_REPLY.fullmatch(path)
        )
    return method in {"PATCH", "DELETE"} and bool(_SESSION.fullmatch(path))


def _durable_operation(path: str) -> bool:
    return bool(
        re.fullmatch(rf"session/{_NATIVE_ID}/(?:prompt_async|abort)$", path)
        or _INTERACTION_REPLY.fullmatch(path)
    )


def _native_path(agent_id: str, path: str) -> str:
    return f"/agents/{agent_id}/opencode/{path}"


def _artifact_params(request: Request, resource_path: str) -> None:
    if resource_path not in _ARTIFACT_PATHS:
        return
    session_id = request.query_params.get("sessionID")
    relative_path = request.query_params.get("path")
    if not session_id or not re.fullmatch(_NATIVE_ID, session_id):
        raise HTTPException(422, "Artifact request requires a valid sessionID")
    if (
        not relative_path
        or relative_path.startswith("/")
        or any(part in {"", ".", ".."} for part in relative_path.split("/"))
        or "directory" in request.query_params
    ):
        raise HTTPException(422, "Artifact request requires a relative path")


def _authorized_actor(request: Request, organization_id: str) -> Actor:
    user = auth.require_unsafe_request(request)
    auth.require_member(request, organization_id)
    return Actor(kind="human", id=user.id, name=user.display_name)


async def _body(request: Request) -> object:
    raw = await request.body()
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(422, "Native operation requires a JSON body") from None
    if not isinstance(value, dict):
        raise HTTPException(422, "Native operation requires an object body")
    if "author" in value:
        raise HTTPException(422, "Native operation does not accept an author body field")
    return value


def _response(reply: HostResponse, idempotency_key: str | None = None) -> Response:
    headers = {"Cache-Control": "no-store"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return Response(
        content=reply.content,
        status_code=reply.status_code,
        media_type=reply.content_type,
        headers=headers,
    )


def _pinned_sessions(reply: HostResponse, pinned: list[str]) -> HostResponse:
    """Keep host recency order while projecting this member's pins first."""
    if not pinned or not 200 <= reply.status_code < 300:
        return reply
    try:
        sessions = json.loads(reply.content)
    except json.JSONDecodeError:
        return reply
    if not isinstance(sessions, list):
        return reply
    pinned_ids = set(pinned)
    ordered = [
        session
        for session in sessions
        if isinstance(session, dict) and session.get("id") in pinned_ids
    ]
    ordered.extend(
        session
        for session in sessions
        if not isinstance(session, dict) or session.get("id") not in pinned_ids
    )
    return HostResponse(
        reply.status_code,
        json.dumps(ordered, separators=(",", ":")).encode(),
        reply.content_type,
    )


@router.api_route("/{resource_path:path}", methods=["GET", "POST", "PATCH", "DELETE"])
async def facade(
    request: Request, organization_id: UUID, agent_id: UUID, resource_path: str
) -> Response:
    if not _allowed(request.method, resource_path):
        raise HTTPException(404, "OpenCode operation not found")
    organization, agent = str(organization_id), str(agent_id)
    if resource_path == "event":
        return await _events(request, organization, agent)
    mutation = request.method != "GET"
    actor = _authorized_actor(request, organization) if mutation else None
    user = None
    if not mutation:
        auth.require_member(request, organization)
        if resource_path in {"session", "experimental/session"}:
            user = auth.current_user(request)
    _artifact_params(request, resource_path)
    body = await _body(request) if mutation else None
    idempotency_key = None
    headers: dict[str, str] = {}
    if actor is not None:
        headers["X-Fesnyng-Actor"] = actor.model_dump_json()
    if _durable_operation(resource_path):
        idempotency_key = request.headers.get("Idempotency-Key") or str(uuid4())
        headers["Idempotency-Key"] = idempotency_key
        if _INTERACTION_REPLY.fullmatch(resource_path) and resource_path.endswith("/reply"):
            assert isinstance(body, dict)
            body = {**body, "operation_id": idempotency_key}
    client = host_client(request)
    with host_errors():
        assigned = client.agents.get_agent(organization, agent)
        reply = await client.raw_request(
            organization,
            assigned["host_id"],
            _native_path(agent, resource_path),
            method=request.method,
            body=body,
            headers=headers,
            params=dict(request.query_params),
        )
    if user is not None:
        reply = _pinned_sessions(
            reply, auth.get_store(request).list_thread_pins(organization, user.id, agent)
        )
    return _response(reply, idempotency_key)


async def _events(request: Request, organization_id: str, agent_id: str) -> Response:
    auth.require_member(request, organization_id)
    session_settings = auth.get_session_settings(request)
    session_token = request.cookies.get(session_settings.cookie_name)
    user = auth.current_user(request)
    client = host_client(request)
    with host_errors():
        assigned = client.agents.get_agent(organization_id, agent_id)
        stream = await client.stream(
            organization_id,
            assigned["host_id"],
            _native_path(agent_id, "event"),
            params=dict(request.query_params),
        )
    if not 200 <= stream.response.status_code < 300:
        try:
            content = await stream.response.aread()
            response = HostResponse(
                stream.response.status_code, content, stream.response.headers.get("content-type")
            )
        finally:
            await stream.close()
        return _response(response)
    content_type = stream.response.headers.get("content-type", "")
    if not content_type.startswith("text/event-stream"):
        await stream.close()
        raise HTTPException(503, "Agent host returned an invalid event stream")
    return StreamingResponse(
        _relay_events(stream, request, session_token, user.id, organization_id),
        media_type=content_type,
        headers={"Cache-Control": "no-store"},
    )


async def _relay_events(
    stream: HostStream,
    request: Request,
    session_token: str | None,
    user_id: str,
    organization_id: str,
) -> AsyncIterator[bytes]:
    try:
        async for chunk in stream.response.aiter_bytes():
            _still_authorized(request, session_token, user_id, organization_id)
            yield chunk
    except httpx.TransportError:
        # Finish the response so the maintained client reconnects and refreshes history.
        return
    finally:
        await stream.close()


def _still_authorized(
    request: Request, session_token: str | None, user_id: str, organization_id: str
) -> None:
    user = auth.get_store(request).user_for_session(session_token) if session_token else None
    if user is None or user.id != user_id:
        raise HTTPException(401, "Authentication required.")
    try:
        auth.get_store(request).membership_for(user_id, organization_id)
    except LookupError:
        raise HTTPException(404, "Organization not found.") from None
