"""Scoped Codex App Server workspace facade.

Codex owns its thread and turn representation.  This module deliberately
forwards that representation instead of translating it into OpenCode parts;
the browser projection is the compatibility boundary.  Fesnyng still owns the
mapped thread scope, human provenance, and durable dispatch receipts.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from typing import Annotated, Any, Literal
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Body, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import Field, ValidationError, model_validator

from fesnyng_backend.agent_models import Contract
from fesnyng_backend.codex_history import full_turns, is_unmaterialized
from fesnyng_backend.host_models import Actor, NativeID, SessionCreate
from fesnyng_backend.host_routes import host_errors, require_binding
from fesnyng_backend.host_runtime import RuntimeUnavailable
from fesnyng_backend.host_workspace import SessionUpdate, Workspace, session_create_kwargs
from fesnyng_backend.host_workspace_lifecycle import workspace_history_snapshot

router = APIRouter(prefix="/organizations/{organization_id}", tags=["codex workspace"])


class CodexPrompt(Contract):
    text: str = Field(default="", max_length=200_000)
    command: str | None = Field(default=None, max_length=128, pattern=r"^[A-Za-z0-9_/-]+$")
    mode: Literal["queued", "steering"] = "queued"

    @model_validator(mode="after")
    def require_content(self):
        if not self.text.strip() and not self.command:
            raise ValueError("Prompt requires text or a configured skill")
        return self


class Stop(Contract):
    cancel_queued: bool = False


class CodexReply(Contract):
    response: dict[str, Any]


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


def _codex(request: Request, organization_id: str, agent_id: str, session_id: str | None = None):
    """Get the explicitly bound App Server adapter, never the OpenCode fallback."""
    runtime = request.app.state.host_runtime
    session = (
        request.app.state.host_store.session(organization_id, agent_id, session_id)
        if session_id is not None
        else None
    )
    if session is not None and session.get("runtime_type") != "codex":
        raise RuntimeUnavailable("Thread is bound to the OpenCode harness")
    router = getattr(runtime, "runtime_router", None)
    adapter = (
        router.for_session(session)
        if router is not None and session is not None
        else getattr(runtime, "codex", None)
    )
    if adapter is None or not all(
        hasattr(adapter, name) for name in ("call", "pending", "respond", "events")
    ):
        raise RuntimeUnavailable("Codex harness is not available on this host")
    return adapter, session


def _record(
    session: Mapping[str, Any], native: object | None = None, *, updated: int | None = None
) -> dict[str, Any]:
    """Present the host mapping as the stable navigation record."""
    title = session["title"]
    if isinstance(native, Mapping) and isinstance(native.get("title"), str) and native["title"]:
        title = native["title"]
    time: dict[str, int] = {"created": session["created_at"] * 1000}
    if updated is not None:
        time["updated"] = updated
    if session.get("archived_at") is not None:
        time["archived"] = session["archived_at"]
    return {
        "id": session["session_id"],
        "runtime_type": session["runtime_type"],
        "frozen": session.get("frozen_at") is not None,
        "frozen_at": session.get("frozen_at"),
        "title": title,
        "directory": session["directory"],
        "time": time,
    }


async def _thread(
    adapter: Any, organization_id: str, agent_id: str, session: Mapping[str, Any]
) -> dict[str, Any]:
    reply = await adapter.call(
        organization_id, agent_id, "thread/read", {"threadId": session["session_id"]}
    )
    thread = reply.get("thread") if isinstance(reply, Mapping) else None
    if not isinstance(thread, Mapping) or thread.get("id") != session["session_id"]:
        raise RuntimeUnavailable("Codex thread receipt is invalid")
    return dict(thread)


def _event_thread_id(notification: object) -> str | None:
    if not isinstance(notification, Mapping):
        return None
    params = notification.get("params")
    if not isinstance(params, Mapping):
        return None
    for key in ("threadId", "thread_id"):
        if isinstance(params.get(key), str):
            return params[key]
    thread = params.get("thread")
    return (
        thread.get("id")
        if isinstance(thread, Mapping) and isinstance(thread.get("id"), str)
        else None
    )


@router.get("/agents/{agent_id}/codex/session")
def sessions(request: Request, organization_id: UUID, agent_id: UUID):
    org, agent = str(organization_id), str(agent_id)
    require_binding(request, org)
    with host_errors():
        incoming = request.app.state.dispatch_store.latest_incoming_at(org, agent)
        rows = [
            session
            for session in request.app.state.host_store.sessions(org, agent)
            if session["runtime_type"] == "codex"
        ]
        rows.sort(
            key=lambda session: (
                -incoming.get(session["session_id"], session["created_at"] * 1000),
                session["session_id"],
            )
        )
        return [
            _record(
                session, updated=incoming.get(session["session_id"], session["created_at"] * 1000)
            )
            for session in rows
        ]


@router.get("/agents/{agent_id}/codex/experimental/session")
def archived_sessions(
    request: Request, organization_id: UUID, agent_id: UUID, archived: bool = False
):
    org, agent = str(organization_id), str(agent_id)
    require_binding(request, org)
    with host_errors():
        incoming = request.app.state.dispatch_store.latest_incoming_at(org, agent)
        rows = [
            session
            for session in request.app.state.host_store.sessions(
                org, agent, archived=None if archived else False
            )
            if session["runtime_type"] == "codex"
        ]
        rows.sort(
            key=lambda session: (
                -incoming.get(session["session_id"], session["created_at"] * 1000),
                session["session_id"],
            )
        )
        return [
            _record(
                session, updated=incoming.get(session["session_id"], session["created_at"] * 1000)
            )
            for session in rows
        ]


@router.post("/agents/{agent_id}/codex/session", status_code=201)
async def create_session(
    request: Request,
    organization_id: UUID,
    agent_id: UUID,
    body: Annotated[SessionCreate | None, Body()] = None,
    actor: Annotated[str | None, Header(alias="X-Fesnyng-Actor")] = None,
):
    org, agent = str(organization_id), str(agent_id)
    _mutation(request, org, actor)
    automatic_title = body is None or "title" not in body.model_fields_set
    title = body.title if body is not None else "New thread"
    envelope = request.app.state.interactions._applied_envelope(org, agent)
    if envelope.configuration.runtime_type != "codex":
        raise HTTPException(409, "Agent is not configured for the Codex harness")
    workspace = (
        body.workspace
        if body is not None and "workspace" in body.model_fields_set
        else envelope.configuration.workspace
    )
    with host_errors():
        created = await request.app.state.host_runtime.create_session(
            org,
            agent,
            title,
            workspace,
            automatic_title=automatic_title,
            **session_create_kwargs(body),
        )
        session_id = created.get("id") if isinstance(created, Mapping) else None
        if not isinstance(session_id, str):
            raise RuntimeUnavailable("Codex thread creation receipt is invalid")
        session = request.app.state.host_store.session(org, agent, session_id)
        if session["runtime_type"] != "codex":
            raise RuntimeUnavailable("Codex thread creation did not preserve its harness binding")
        return _record(session, created)


@router.get("/agents/{agent_id}/codex/command")
async def commands(request: Request, organization_id: UUID, agent_id: UUID):
    """Expose configured explicit skills only after native inventory proves them."""
    org, agent = str(organization_id), str(agent_id)
    require_binding(request, org)
    with host_errors():
        adapter, _ = _codex(request, org, agent)
        envelope = request.app.state.interactions._applied_envelope(org, agent)
        expected = {skill.name for skill in envelope.configuration.skills if skill.explicit_only}
        reply = await adapter.call(org, agent, "skills/list", {"forceReload": True})
        data = reply.get("data") if isinstance(reply, Mapping) else None
        if not isinstance(data, list):
            raise RuntimeUnavailable("Codex skills inventory receipt is invalid")
        available: dict[str, str] = {}
        for entry in data:
            if not isinstance(entry, Mapping) or not isinstance(entry.get("skills"), list):
                raise RuntimeUnavailable("Codex skills inventory receipt is invalid")
            for skill in entry["skills"]:
                if not isinstance(skill, Mapping):
                    raise RuntimeUnavailable("Codex skills inventory receipt is invalid")
                name, path = skill.get("name"), skill.get("path")
                if (
                    name in expected
                    and isinstance(path, str)
                    and path.endswith(f"/{name}/SKILL.md")
                ):
                    description = skill.get("description")
                    available[name] = description if isinstance(description, str) else name
        if available.keys() != expected:
            raise RuntimeUnavailable("Configured Codex skills are not available natively")
        return [
            {"name": f"fesnyng/{name}", "description": available[name]}
            for name in sorted(available)
        ]


def _workspace(request: Request) -> Workspace:
    return Workspace(
        request.app.state.host_store,
        request.app.state.host_runtime,
        request.app.state.dispatch_store,
        request.app.state.interactions,
    )


@router.get("/agents/{agent_id}/codex/file")
async def artifact_list(
    request: Request,
    organization_id: UUID,
    agent_id: UUID,
    session_id: Annotated[NativeID, Query(alias="sessionID")],
    path: Annotated[str, Query(max_length=240)],
):
    org, agent = str(organization_id), str(agent_id)
    require_binding(request, org)
    with host_errors():
        session = request.app.state.host_store.session(org, agent, session_id)
        if session["runtime_type"] != "codex":
            raise RuntimeUnavailable("Thread is bound to the OpenCode harness")
        return await _workspace(request).files(org, agent, session_id, path)


@router.get("/agents/{agent_id}/codex/file/content")
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
        session = request.app.state.host_store.session(org, agent, session_id)
        if session["runtime_type"] != "codex":
            raise RuntimeUnavailable("Thread is bound to the OpenCode harness")
        return await _workspace(request).preview(org, agent, session_id, path)


@router.get("/agents/{agent_id}/codex/file/download")
async def artifact_download(
    request: Request,
    organization_id: UUID,
    agent_id: UUID,
    session_id: Annotated[NativeID, Query(alias="sessionID")],
    path: Annotated[str, Query(min_length=1, max_length=240)],
):
    org, agent = str(organization_id), str(agent_id)
    require_binding(request, org)
    workspace = _workspace(request)
    with host_errors():
        session = request.app.state.host_store.session(org, agent, session_id)
        if session["runtime_type"] != "codex":
            raise RuntimeUnavailable("Thread is bound to the OpenCode harness")
        context = workspace.download(org, agent, session_id, path)
        _metadata, stream = await context.__aenter__()
    filename = path.rsplit("/", 1)[-1]
    fallback = "".join(
        char if 32 <= ord(char) < 127 and char not in '\\\\"' else "_" for char in filename
    )
    encoded = quote(filename, safe="")

    async def body():
        try:
            async for chunk in stream:
                yield chunk
        finally:
            await context.__aexit__(None, None, None)

    return StreamingResponse(
        body(),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename=\"{fallback or 'download'}\"; filename*=UTF-8''{encoded}"
        },
    )


@router.get("/agents/{agent_id}/codex/session/{session_id}")
async def get_session(
    request: Request, organization_id: UUID, agent_id: UUID, session_id: NativeID
):
    org, agent = str(organization_id), str(agent_id)
    require_binding(request, org)
    with host_errors():
        workspace = _workspace(request)
        session = await workspace._scoped_session(org, agent, session_id)
        if session.get("frozen_at") is not None:
            if session["runtime_type"] != "codex":
                raise RuntimeUnavailable("Thread is bound to the OpenCode harness")
            snapshot = workspace._snapshot(org, agent, session)
            return _record(session, snapshot["session"])
        adapter, session = _codex(request, org, agent, session_id)
        return _record(session, await _thread(adapter, org, agent, session))


@router.get("/agents/{agent_id}/codex/session/{session_id}/history")
async def history(request: Request, organization_id: UUID, agent_id: UUID, session_id: NativeID):
    org, agent = str(organization_id), str(agent_id)
    require_binding(request, org)
    with host_errors():
        workspace = _workspace(request)
        session = await workspace._scoped_session(org, agent, session_id)
        if session.get("frozen_at") is not None:
            if session["runtime_type"] != "codex":
                raise RuntimeUnavailable("Thread is bound to the OpenCode harness")
            return workspace._snapshot(org, agent, session)["history"]
        binding = request.app.state.host_store.workspace_binding(org, agent, session_id)
        if binding is not None and binding["state"] != "ready":
            captured = workspace_history_snapshot(binding, session)
            history = captured.get("history") if isinstance(captured, Mapping) else None
            if not isinstance(history, Mapping):
                raise RuntimeUnavailable("Workspace lifecycle history receipt is unavailable")
            return dict(history)
        adapter, _session = _codex(request, org, agent, session_id)
        try:
            reply = await adapter.call(
                org,
                agent,
                "thread/read",
                {"threadId": session_id, "includeTurns": True},
            )
        except RuntimeUnavailable as error:
            if not is_unmaterialized(error, session_id):
                raise
            metadata = await _thread(adapter, org, agent, _session)
            return {
                "thread": metadata,
                "turns": [],
                "historyState": "unavailable",
                "reason": "Pinned Codex App Server cannot page this thread yet",
            }
        thread = reply.get("thread") if isinstance(reply, Mapping) else None
        if not isinstance(thread, Mapping) or thread.get("id") != session_id:
            raise RuntimeUnavailable("Codex thread history receipt is invalid")
        turns = await full_turns(adapter, org, agent, session_id)
        return {
            "thread": dict(thread),
            "turns": turns,
            "historyState": "complete",
        }


@router.patch("/agents/{agent_id}/codex/session/{session_id}")
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
    if body.title is None and body.time is None:
        raise HTTPException(409, "Codex session update requires a title or archive state")
    if body.title is not None and body.time is not None:
        raise HTTPException(409, "Codex session updates one native field at a time")
    with host_errors():
        async with request.app.state.host_runtime.lock(agent):
            request.app.state.host_store.require_writable(org, agent, session_id)
            adapter, _session = _codex(request, org, agent, session_id)
            if body.title is not None:
                reply = await adapter.call(
                    org, agent, "thread/name/set", {"threadId": session_id, "name": body.title}
                )
                if not isinstance(reply, Mapping):
                    raise RuntimeUnavailable("Codex thread rename receipt is invalid")
                request.app.state.host_store.rename_session(org, agent, session_id, body.title)
            else:
                assert body.time is not None
                archived_at = body.time.archived
                method = "thread/archive" if archived_at is not None else "thread/unarchive"
                reply = await adapter.call(org, agent, method, {"threadId": session_id})
                if not isinstance(reply, Mapping):
                    raise RuntimeUnavailable("Codex thread archive receipt is invalid")
                request.app.state.host_store.archive_session(org, agent, session_id, archived_at)
            return _record(
                request.app.state.host_store.session(org, agent, session_id),
                {"title": body.title} if body.title is not None else None,
            )


@router.delete("/agents/{agent_id}/codex/session/{session_id}", status_code=204)
async def delete_session(
    request: Request,
    organization_id: UUID,
    agent_id: UUID,
    session_id: NativeID,
    actor: Annotated[str | None, Header(alias="X-Fesnyng-Actor")] = None,
):
    org = str(organization_id)
    _mutation(request, org, actor)
    raise HTTPException(409, "Codex thread deletion is not supported")


@router.post("/agents/{agent_id}/codex/session/{session_id}/prompt", status_code=202)
def prompt(
    request: Request,
    organization_id: UUID,
    agent_id: UUID,
    session_id: NativeID,
    body: CodexPrompt,
    actor: Annotated[str | None, Header(alias="X-Fesnyng-Actor")] = None,
    operation: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    from fesnyng_backend.host_dispatch import HostSubmission

    org, agent = str(organization_id), str(agent_id)
    author, operation_id = _mutation(request, org, actor), _operation(operation)
    with host_errors():
        _codex(request, org, agent, session_id)
        receipt = request.app.state.dispatch_store.enqueue(
            org,
            agent,
            session_id,
            HostSubmission(
                id=operation_id,
                text=body.text,
                command=body.command,
                mode=body.mode,
                author=author,
            ),
            author,
        )
    request.app.state.dispatcher.wake()
    return receipt


@router.post("/agents/{agent_id}/codex/session/{session_id}/abort", status_code=202)
def abort(
    request: Request,
    organization_id: UUID,
    agent_id: UUID,
    session_id: NativeID,
    body: Annotated[Stop | None, Body()] = None,
    actor: Annotated[str | None, Header(alias="X-Fesnyng-Actor")] = None,
    operation: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    from fesnyng_backend.host_dispatch import HostSubmission

    org, agent = str(organization_id), str(agent_id)
    author, operation_id = _mutation(request, org, actor), _operation(operation)
    with host_errors():
        _codex(request, org, agent, session_id)
        receipt = request.app.state.dispatch_store.enqueue(
            org,
            agent,
            session_id,
            HostSubmission(
                id=operation_id,
                mode="stop",
                cancel_queued=(body or Stop()).cancel_queued,
                author=author,
            ),
            author,
        )
    request.app.state.dispatcher.wake()
    return receipt


@router.get("/agents/{agent_id}/codex/pending")
async def pending(
    request: Request, organization_id: UUID, agent_id: UUID, sessionID: NativeID | None = None
):
    org, agent = str(organization_id), str(agent_id)
    require_binding(request, org)
    with host_errors():
        sessions = (
            [request.app.state.host_store.session(org, agent, sessionID)]
            if sessionID
            else request.app.state.host_store.sessions(org, agent)
        )
        result: list[dict[str, Any]] = []
        for session in sessions:
            if session["runtime_type"] != "codex" or session.get("frozen_at") is not None:
                continue
            adapter, _ = _codex(request, org, agent, session["session_id"])
            entries = await adapter.pending(org, agent, session["session_id"])
            if not isinstance(entries, list) or not all(
                isinstance(entry, dict) for entry in entries
            ):
                raise RuntimeUnavailable("Codex pending request receipt is invalid")
            result.extend(entries)
        return result


@router.post("/agents/{agent_id}/codex/pending/{request_id}/reply")
async def reply(
    request: Request,
    organization_id: UUID,
    agent_id: UUID,
    request_id: str,
    body: CodexReply,
    sessionID: NativeID,
    actor: Annotated[str | None, Header(alias="X-Fesnyng-Actor")] = None,
    operation: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    org, agent = str(organization_id), str(agent_id)
    author, operation_id = _mutation(request, org, actor), _operation(operation)
    with host_errors():
        _codex(request, org, agent, sessionID)
        return await request.app.state.interactions.reply_codex(
            org, agent, sessionID, operation_id, request_id, body.response, author
        )


@router.get("/agents/{agent_id}/codex/event")
async def events(request: Request, organization_id: UUID, agent_id: UUID):
    org, agent = str(organization_id), str(agent_id)
    require_binding(request, org)
    with host_errors():
        adapter, _ = _codex(request, org, agent)

    guard = request.app.state.maintenance_guard
    with host_errors():
        guard.require_events_open()

    async def stream() -> AsyncIterator[str]:
        async for notification in adapter.events(org, agent):
            thread_id = _event_thread_id(notification)
            if thread_id is None:
                continue
            try:
                session = request.app.state.host_store.session(org, agent, thread_id)
            except LookupError:
                continue
            if session["runtime_type"] != "codex" or session.get("frozen_at") is not None:
                continue
            if notification.get("method") == "thread/name/updated":
                params = notification.get("params")
                title = params.get("threadName") if isinstance(params, Mapping) else None
                if isinstance(title, str) and title:
                    # Commit the host projection before forwarding the native
                    # notification; list/header reload then cannot observe old data.
                    request.app.state.host_store.complete_title_generation(
                        org, agent, thread_id, title[:100]
                    )
            yield f"event: message\ndata: {json.dumps(notification, separators=(',', ':'))}\n\n"

    return StreamingResponse(guard.event_stream(stream()), media_type="text/event-stream")
