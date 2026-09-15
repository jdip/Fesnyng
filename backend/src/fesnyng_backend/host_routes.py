"""Explicit authenticated host operations, scoped to installation-bound organizations."""

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from fesnyng_backend.agent_lifecycle import HostLifecycleRequest
from fesnyng_backend.host_models import (
    HarnessSwitch,
    HostAgentConfiguration,
    SessionCreate,
    SessionProjectProvenance,
)
from fesnyng_backend.host_runtime import RuntimeUnavailable

router = APIRouter(prefix="/organizations/{organization_id}", tags=["host"])


def require_binding(request: Request, organization_id: str) -> None:
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "Host binding authentication required")
    organization = request.app.state.host_store.authenticate(token)
    if organization is None:
        raise HTTPException(401, "Host binding authentication required")
    if organization != organization_id:
        raise HTTPException(403, "Host binding does not authorize this organization")


@contextmanager
def host_errors() -> Iterator[None]:
    try:
        yield
    except LookupError:
        raise HTTPException(404, "Host resource not found") from None
    except PermissionError:
        raise HTTPException(403, "Host operation not permitted") from None
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    except RuntimeUnavailable as error:
        raise HTTPException(503, str(error)) from None


@router.get("/agents/{agent_id}")
async def agent_status(request: Request, organization_id: UUID, agent_id: UUID):
    require_binding(request, str(organization_id))
    with host_errors():
        return await request.app.state.agent_lifecycle.status(str(organization_id), str(agent_id))


@router.post("/agents/{agent_id}/lifecycle")
async def lifecycle(
    request: Request, organization_id: UUID, agent_id: UUID, body: HostLifecycleRequest
):
    org, aid = str(organization_id), str(agent_id)
    require_binding(request, org)
    with host_errors():
        return await request.app.state.agent_lifecycle.perform(org, aid, body)


@router.put("/agents/{agent_id}")
async def apply_agent(
    request: Request, organization_id: UUID, agent_id: UUID, body: HostAgentConfiguration
):
    org = str(organization_id)
    require_binding(request, org)
    if body.organization_id != organization_id or body.agent_id != agent_id:
        raise HTTPException(409, "Configuration identity mismatch")
    with host_errors():
        return await request.app.state.host_configuration.apply(body)


@router.post("/agents/{agent_id}/harness-switch")
async def switch_harness(
    request: Request, organization_id: UUID, agent_id: UUID, body: HarnessSwitch
):
    org, aid = str(organization_id), str(agent_id)
    require_binding(request, org)
    with host_errors():
        return await request.app.state.host_configuration.switch_harness(
            org, aid, body.expected_version, body.target_runtime_type
        )


@router.post("/agents/{agent_id}/replace")
async def replace_agent(request: Request, organization_id: UUID, agent_id: UUID):
    org, aid = str(organization_id), str(agent_id)
    require_binding(request, org)
    with host_errors():
        if any(
            receipt["organization_id"] == org
            and receipt["agent_id"] == aid
            and receipt["state"] != "queued"
            for receipt in request.app.state.dispatch_store.pending()
        ):
            raise RuntimeUnavailable("Agent replacement needs delivery reconciliation")
        await request.app.state.host_runtime.replace(org, aid)
        return request.app.state.host_store.agent_status(org, aid)


@router.post("/agents/{agent_id}/sessions", status_code=201)
async def create_session(
    request: Request, organization_id: UUID, agent_id: UUID, body: SessionCreate
):
    org, aid = str(organization_id), str(agent_id)
    require_binding(request, org)
    with host_errors():
        return await request.app.state.host_runtime.create_session(
            org, aid, body.title, body.workspace
        )


@router.get("/agents/{agent_id}/sessions")
def sessions(request: Request, organization_id: UUID, agent_id: UUID):
    org, aid = str(organization_id), str(agent_id)
    require_binding(request, org)
    with host_errors():
        return request.app.state.host_store.sessions(org, aid)


@router.put("/agents/{agent_id}/sessions/{session_id}/project-provenance")
def set_project_provenance(
    request: Request,
    organization_id: UUID,
    agent_id: UUID,
    session_id: str,
    body: SessionProjectProvenance,
):
    org, aid = str(organization_id), str(agent_id)
    require_binding(request, org)
    with host_errors():
        request.app.state.host_store.set_session_project_provenance(
            org, aid, session_id, str(body.project_id) if body.project_id else None
        )
        session = request.app.state.host_store.session(org, aid, session_id)
        session.pop("project_provenance_initialized", None)
        return session


@router.get("/agents/{agent_id}/sessions/{session_id}/messages")
async def messages(request: Request, organization_id: UUID, agent_id: UUID, session_id: str):
    org, aid = str(organization_id), str(agent_id)
    require_binding(request, org)
    with host_errors():
        session = request.app.state.host_store.session(org, aid, session_id)
        # Keep the original generic read URL on the same scoped history owner
        # as each harness facade, including host-owned frozen snapshots.
        if session["runtime_type"] == "codex":
            from fesnyng_backend.host_codex_routes import history

            return await history(request, organization_id, agent_id, session_id)
        from fesnyng_backend.host_workspace_routes import messages as opencode_messages

        return await opencode_messages(request, organization_id, agent_id, session_id)
