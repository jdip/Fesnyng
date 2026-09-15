"""Human-authorized forwarding for the scoped Codex workspace facade."""

from __future__ import annotations

import json
import re
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from fesnyng_backend import auth, projects
from fesnyng_backend.control_host_routes import host_client, host_errors
from fesnyng_backend.host_client import HostResponse
from fesnyng_backend.host_models import Actor

router = APIRouter(
    prefix="/organizations/{organization_id}/agents/{agent_id}/codex", tags=["codex workspace"]
)

_ID = r"[A-Za-z0-9_-]{1,160}"
_SESSION = re.compile(rf"^session/{_ID}$")
_READ = re.compile(rf"^session/{_ID}/history$")
_MUTATION = re.compile(rf"^session/{_ID}/(?:prompt|abort)$")
_PENDING_REPLY = re.compile(rf"^pending/{_ID}/reply$")


def _allowed(method: str, path: str) -> bool:
    if method == "GET":
        return path in {
            "session",
            "experimental/session",
            "pending",
            "event",
            "command",
            "file",
            "file/content",
            "file/download",
        } or bool(_SESSION.fullmatch(path) or _READ.fullmatch(path))
    if method == "POST":
        return path == "session" or bool(
            _MUTATION.fullmatch(path) or _PENDING_REPLY.fullmatch(path)
        )
    return method in {"PATCH", "DELETE"} and bool(_SESSION.fullmatch(path))


def _durable(path: str) -> bool:
    return bool(_MUTATION.fullmatch(path) or _PENDING_REPLY.fullmatch(path))


def _actor(request: Request, org: str) -> Actor:
    user = auth.require_unsafe_request(request)
    auth.require_member(request, org)
    return Actor(kind="human", id=user.id, name=user.display_name)


async def _body(request: Request) -> object:
    raw = await request.body()
    if not raw:
        return None
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(422, "Codex operation requires a JSON body") from None
    if not isinstance(body, dict) or "author" in body:
        raise HTTPException(422, "Codex operation requires an object body")
    return body


def _response(reply: HostResponse, operation: str | None = None) -> Response:
    headers = {"Cache-Control": "no-store"}
    if operation:
        headers["Idempotency-Key"] = operation
    return Response(
        content=reply.content,
        status_code=reply.status_code,
        media_type=reply.content_type,
        headers=headers,
    )


@router.api_route("/{resource_path:path}", methods=["GET", "POST", "PATCH", "DELETE"])
async def proxy(request: Request, organization_id: UUID, agent_id: UUID, resource_path: str):
    if not _allowed(request.method, resource_path):
        raise HTTPException(404, "Codex operation not found")
    org, agent = str(organization_id), str(agent_id)
    mutation = request.method != "GET"
    actor = _actor(request, org) if mutation else None
    if not mutation:
        auth.require_member(request, org)
    body = await _body(request) if mutation else None
    project_id = (
        projects.project_id_for_native_session_create(body) if resource_path == "session" else None
    )
    if project_id is not None:
        try:
            projects.require_project_for_native_session_create(request, org, project_id)
        except projects.RepositoryWorkspaceUnavailable as error:
            raise HTTPException(409, str(error)) from None
        except (LookupError, ValueError) as error:
            raise HTTPException(422, str(error)) from None
    operation: str | None = None
    headers: dict[str, str] = {}
    if actor:
        headers["X-Fesnyng-Actor"] = actor.model_dump_json()
    if _durable(resource_path):
        operation = request.headers.get("Idempotency-Key") or str(uuid4())
        headers["Idempotency-Key"] = operation
    client = host_client(request)
    with host_errors():
        assigned = client.agents.get_agent(org, agent)
        if resource_path == "event":
            stream = await client.stream(org, assigned["host_id"], f"/agents/{agent}/codex/event")
            return StreamingResponse(
                _relay(stream),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-store"},
            )
        reply = await client.raw_request(
            org,
            assigned["host_id"],
            f"/agents/{agent}/codex/{resource_path}",
            method=request.method,
            body=body,
            headers=headers,
            params=dict(request.query_params),
        )
    reply = await projects.attach_created_native_session_project(
        request, org, agent, project_id, reply, _ID
    )
    if resource_path in {"session", "experimental/session"} and request.method == "GET":
        reply = projects.reconcile_host_session_response(request, org, agent, reply)
    return _response(reply, operation)


async def _relay(stream):
    try:
        async for chunk in stream.response.aiter_raw():
            yield chunk
    finally:
        await stream.close()
