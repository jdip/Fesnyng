"""Authenticated, allowlisted host façade over scoped OpenCode workspace operations."""

from __future__ import annotations

import asyncio
import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from fesnyng_backend.agent_models import Contract
from fesnyng_backend.host_models import Actor, NativeID, SessionCreate
from fesnyng_backend.host_routes import host_errors, require_binding
from fesnyng_backend.host_workspace import (
    Fork,
    PermissionReply,
    Prompt,
    QuestionReply,
    Revert,
    SessionUpdate,
    Workspace,
    _event_session_id,
)

router = APIRouter(prefix="/organizations/{organization_id}", tags=["workspace"])


class Stop(Contract):
    cancel_queued: bool = False


def _workspace(request: Request) -> Workspace:
    return Workspace(
        request.app.state.host_store,
        request.app.state.host_runtime,
        request.app.state.dispatch_store,
        request.app.state.interactions,
    )


def _actor(header: str | None) -> Actor:
    if not header:
        raise HTTPException(400, "Trusted actor provenance is required")
    try:
        return Actor.model_validate_json(header)
    except ValidationError:
        raise HTTPException(400, "Trusted actor provenance is invalid") from None


def _operation(header: str | None) -> UUID:
    if not header:
        raise HTTPException(400, "Idempotency-Key is required")
    try:
        return UUID(header)
    except ValueError:
        raise HTTPException(400, "Idempotency-Key must be a UUID") from None


def _mutation(request: Request, organization_id: str, actor: str | None) -> Actor:
    require_binding(request, organization_id)
    return _actor(actor)


@router.get("/agents/{agent_id}/opencode/session")
def sessions(request: Request, organization_id: UUID, agent_id: UUID):
    org, agent = str(organization_id), str(agent_id)
    require_binding(request, org)
    with host_errors():
        return _workspace(request).sessions(org, agent)


@router.get("/agents/{agent_id}/opencode/experimental/session")
def experimental_sessions(
    request: Request,
    organization_id: UUID,
    agent_id: UUID,
    archived: bool = False,
):
    """Only mapped roots, never the native global session index."""
    org, agent = str(organization_id), str(agent_id)
    require_binding(request, org)
    with host_errors():
        return _workspace(request).sessions(org, agent, archived=archived)


@router.post("/agents/{agent_id}/opencode/session", status_code=201)
async def create_session(
    request: Request,
    organization_id: UUID,
    agent_id: UUID,
    body: Annotated[SessionCreate | None, Body()] = None,
    actor: Annotated[str | None, Header(alias="X-Fesnyng-Actor")] = None,
):
    org, agent = str(organization_id), str(agent_id)
    _mutation(request, org, actor)
    with host_errors():
        return await _workspace(request).create(org, agent, body)


@router.get("/agents/{agent_id}/opencode/session/status")
async def session_status(request: Request, organization_id: UUID, agent_id: UUID):
    org, agent = str(organization_id), str(agent_id)
    require_binding(request, org)
    mapped = request.app.state.host_store.sessions(org, agent)
    by_directory: dict[str, set[str]] = {}
    for session in mapped:
        by_directory.setdefault(session["directory"], set()).add(session["session_id"])
    statuses: dict[str, object] = {}
    with host_errors():
        for directory, ids in by_directory.items():
            native = await request.app.state.host_runtime.request(
                org, agent, "/session/status", directory=directory
            )
            if not isinstance(native, dict):
                raise HTTPException(503, "Native session status response is invalid")
            statuses.update(
                {session_id: native[session_id] for session_id in ids if session_id in native}
            )
    return statuses


@router.get("/agents/{agent_id}/opencode/session/{session_id}")
async def session(request: Request, organization_id: UUID, agent_id: UUID, session_id: NativeID):
    org, agent = str(organization_id), str(agent_id)
    require_binding(request, org)
    with host_errors():
        return await _workspace(request).get(org, agent, session_id)


@router.patch("/agents/{agent_id}/opencode/session/{session_id}")
async def update_session(
    request: Request,
    organization_id: UUID,
    agent_id: UUID,
    session_id: NativeID,
    body: SessionUpdate,
    actor: Annotated[str | None, Header(alias="X-Fesnyng-Actor")] = None,
):
    org, agent = str(organization_id), str(agent_id)
    _mutation(request, org, actor)
    with host_errors():
        return await _workspace(request).update(org, agent, session_id, body)


@router.delete("/agents/{agent_id}/opencode/session/{session_id}", status_code=204)
async def delete_session(
    request: Request,
    organization_id: UUID,
    agent_id: UUID,
    session_id: NativeID,
    actor: Annotated[str | None, Header(alias="X-Fesnyng-Actor")] = None,
):
    org, agent = str(organization_id), str(agent_id)
    _mutation(request, org, actor)
    with host_errors():
        await _workspace(request).delete(org, agent, session_id)
    return Response(status_code=204)


@router.get("/agents/{agent_id}/opencode/session/{session_id}/message")
async def messages(request: Request, organization_id: UUID, agent_id: UUID, session_id: NativeID):
    org, agent = str(organization_id), str(agent_id)
    require_binding(request, org)
    with host_errors():
        return await _workspace(request).messages(org, agent, session_id)


@router.post("/agents/{agent_id}/opencode/session/{session_id}/prompt_async", status_code=202)
def prompt(
    request: Request,
    organization_id: UUID,
    agent_id: UUID,
    session_id: NativeID,
    body: Prompt,
    actor: Annotated[str | None, Header(alias="X-Fesnyng-Actor")] = None,
    operation: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    org, agent = str(organization_id), str(agent_id)
    author, operation_id = _mutation(request, org, actor), _operation(operation)
    with host_errors():
        receipt = _workspace(request).prompt(org, agent, session_id, operation_id, body, author)
    request.app.state.dispatcher.wake()
    return receipt


@router.post("/agents/{agent_id}/opencode/session/{session_id}/abort", status_code=202)
def abort(
    request: Request,
    organization_id: UUID,
    agent_id: UUID,
    session_id: NativeID,
    body: Annotated[Stop | None, Body()] = None,
    actor: Annotated[str | None, Header(alias="X-Fesnyng-Actor")] = None,
    operation: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    org, agent = str(organization_id), str(agent_id)
    author, operation_id = _mutation(request, org, actor), _operation(operation)
    with host_errors():
        receipt = _workspace(request).stop(
            org, agent, session_id, operation_id, (body or Stop()).cancel_queued, author
        )
    request.app.state.dispatcher.wake()
    return receipt


@router.post("/agents/{agent_id}/opencode/session/{session_id}/fork", status_code=201)
async def fork(
    request: Request,
    organization_id: UUID,
    agent_id: UUID,
    session_id: NativeID,
    body: Fork,
    actor: Annotated[str | None, Header(alias="X-Fesnyng-Actor")] = None,
):
    org, agent = str(organization_id), str(agent_id)
    author = _mutation(request, org, actor)
    with host_errors():
        return await _workspace(request).fork(org, agent, session_id, body, author)


@router.post("/agents/{agent_id}/opencode/session/{session_id}/revert")
async def revert(
    request: Request,
    organization_id: UUID,
    agent_id: UUID,
    session_id: NativeID,
    body: Revert,
    actor: Annotated[str | None, Header(alias="X-Fesnyng-Actor")] = None,
):
    org, agent = str(organization_id), str(agent_id)
    _mutation(request, org, actor)
    with host_errors():
        return await _workspace(request).revert(org, agent, session_id, body)


@router.post("/agents/{agent_id}/opencode/session/{session_id}/unrevert")
async def unrevert(
    request: Request,
    organization_id: UUID,
    agent_id: UUID,
    session_id: NativeID,
    actor: Annotated[str | None, Header(alias="X-Fesnyng-Actor")] = None,
):
    org, agent = str(organization_id), str(agent_id)
    _mutation(request, org, actor)
    with host_errors():
        return await _workspace(request).unrevert(org, agent, session_id)


@router.get("/agents/{agent_id}/opencode/question")
async def questions(request: Request, organization_id: UUID, agent_id: UUID):
    org, agent = str(organization_id), str(agent_id)
    require_binding(request, org)
    with host_errors():
        return await _workspace(request).pending_all(org, agent, "question")


@router.get("/agents/{agent_id}/opencode/permission")
async def permissions(request: Request, organization_id: UUID, agent_id: UUID):
    org, agent = str(organization_id), str(agent_id)
    require_binding(request, org)
    with host_errors():
        return await _workspace(request).pending_all(org, agent, "permission")


@router.post("/agents/{agent_id}/opencode/question/{request_id}/reply")
async def reply_question(
    request: Request,
    organization_id: UUID,
    agent_id: UUID,
    request_id: NativeID,
    body: QuestionReply,
    actor: Annotated[str | None, Header(alias="X-Fesnyng-Actor")] = None,
    operation: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    org, agent = str(organization_id), str(agent_id)
    author = _mutation(request, org, actor)
    if _operation(operation) != body.operation_id:
        raise HTTPException(400, "Idempotency-Key must match operation_id")
    with host_errors():
        return await _workspace(request).reply_question(org, agent, request_id, body, author)


@router.post("/agents/{agent_id}/opencode/question/{request_id}/reject")
async def reject_question(
    request: Request,
    organization_id: UUID,
    agent_id: UUID,
    request_id: NativeID,
    actor: Annotated[str | None, Header(alias="X-Fesnyng-Actor")] = None,
    operation: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    org, agent = str(organization_id), str(agent_id)
    author, operation_id = _mutation(request, org, actor), _operation(operation)
    with host_errors():
        return await _workspace(request).reject_question(
            org, agent, request_id, operation_id, author
        )


@router.post("/agents/{agent_id}/opencode/permission/{request_id}/reply")
async def reply_permission(
    request: Request,
    organization_id: UUID,
    agent_id: UUID,
    request_id: NativeID,
    body: PermissionReply,
    actor: Annotated[str | None, Header(alias="X-Fesnyng-Actor")] = None,
    operation: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    org, agent = str(organization_id), str(agent_id)
    author = _mutation(request, org, actor)
    if _operation(operation) != body.operation_id:
        raise HTTPException(400, "Idempotency-Key must match operation_id")
    with host_errors():
        return await _workspace(request).reply_permission(org, agent, request_id, body, author)


@router.get("/agents/{agent_id}/opencode/provider")
def models(request: Request, organization_id: UUID, agent_id: UUID):
    org, agent = str(organization_id), str(agent_id)
    require_binding(request, org)
    with host_errors():
        return _workspace(request).models(org, agent)


@router.get("/agents/{agent_id}/opencode/command")
def commands(request: Request, organization_id: UUID, agent_id: UUID):
    org, agent = str(organization_id), str(agent_id)
    require_binding(request, org)
    with host_errors():
        return _workspace(request).commands(org, agent)


@router.get("/agents/{agent_id}/opencode/config")
def config(request: Request, organization_id: UUID, agent_id: UUID):
    org, agent = str(organization_id), str(agent_id)
    require_binding(request, org)
    with host_errors():
        return _workspace(request).config(org, agent)


def _event_session(data: object) -> str | None:
    return _event_session_id(data)


@router.get("/agents/{agent_id}/opencode/event")
async def events(request: Request, organization_id: UUID, agent_id: UUID):
    org, agent = str(organization_id), str(agent_id)
    require_binding(request, org)
    workspace = _workspace(request)

    async def stream():
        queue: asyncio.Queue[tuple[str, str | None]] = asyncio.Queue()

        async def relay(directory: str):
            try:
                async with request.app.state.host_runtime.event_stream(
                    org, agent, directory
                ) as lines:
                    async for line in lines:
                        if line.startswith("data:"):
                            await queue.put((directory, line[5:].strip()))
            finally:
                await queue.put((directory, None))

        tasks: dict[str, asyncio.Task[None]] = {}
        attached: set[str] = set()

        def attach_new_directories() -> None:
            for session in request.app.state.host_store.sessions(org, agent):
                directory = session["directory"]
                if directory not in attached:
                    attached.add(directory)
                    tasks[directory] = asyncio.create_task(relay(directory))

        try:
            while True:
                attach_new_directories()
                try:
                    directory, raw = await asyncio.wait_for(queue.get(), timeout=0.25)
                except TimeoutError:
                    continue
                if raw is None:
                    # A disposed/restarted native instance has no stream to
                    # relay.  Ending this façade stream makes clients
                    # reconnect and acquire the fresh native stream instead
                    # of remaining silently attached to a dead one.
                    return
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                session_id = _event_session(data)
                if session_id and await workspace.event_authorized(
                    org, agent, session_id, directory
                ):
                    data = workspace.project_event(org, agent, data)
                    yield f"event: message\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"
        finally:
            for task in tasks.values():
                task.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.get("/agents/{agent_id}/opencode/file")
async def artifact_list(
    request: Request,
    organization_id: UUID,
    agent_id: UUID,
    session_id: Annotated[NativeID, Query(alias="sessionID")],
    path: Annotated[str, Query(min_length=1, max_length=240)],
):
    org, agent = str(organization_id), str(agent_id)
    require_binding(request, org)
    with host_errors():
        return await _workspace(request).artifact(org, agent, session_id, path, content=False)


@router.get("/agents/{agent_id}/opencode/file/content")
async def artifact_content(
    request: Request,
    organization_id: UUID,
    agent_id: UUID,
    session_id: Annotated[NativeID, Query(alias="sessionID")],
    path: Annotated[str, Query(min_length=1, max_length=240)],
):
    org, agent = str(organization_id), str(agent_id)
    require_binding(request, org)
    with host_errors():
        return await _workspace(request).artifact(org, agent, session_id, path, content=True)
