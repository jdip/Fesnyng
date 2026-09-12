"""Explicit authenticated host operations, scoped to installation-bound organizations."""

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from fesnyng_backend.host_models import HostAgentConfiguration, SessionCreate
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
        status = request.app.state.host_store.agent_status(str(organization_id), str(agent_id))
        container = await request.app.state.host_runtime.inspect(
            str(organization_id), str(agent_id)
        )
        status["container_state"] = container["state"]["Status"] if container else "missing"
        return status


@router.put("/agents/{agent_id}")
async def apply_agent(
    request: Request, organization_id: UUID, agent_id: UUID, body: HostAgentConfiguration
):
    org, aid = str(organization_id), str(agent_id)
    require_binding(request, org)
    if body.organization_id != organization_id or body.agent_id != agent_id:
        raise HTTPException(409, "Configuration identity mismatch")
    store, runtime = request.app.state.host_store, request.app.state.host_runtime
    with host_errors():
        async with runtime.lock(aid):
            changed = store.stage_agent(body)
            if changed:
                await runtime.assert_quiet(org, aid)
                profile = body.configuration.profile_id
                if profile is not None:
                    request.app.state.credential_store.assign_agent(
                        org, aid, str(profile), store.agent(org, aid)["agent_token"]
                    )
                else:
                    request.app.state.credential_store.unassign_agent(org, aid)
                try:
                    await runtime.configure(body)
                    store.mark_applied(body)
                except RuntimeUnavailable as error:
                    store.set_runtime_state(org, aid, "pending", str(error))
                    raise
            return store.agent_status(org, aid)


@router.post("/agents/{agent_id}/replace")
async def replace_agent(request: Request, organization_id: UUID, agent_id: UUID):
    org, aid = str(organization_id), str(agent_id)
    require_binding(request, org)
    with host_errors():
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


@router.get("/agents/{agent_id}/sessions/{session_id}/messages")
async def messages(request: Request, organization_id: UUID, agent_id: UUID, session_id: str):
    org, aid = str(organization_id), str(agent_id)
    require_binding(request, org)
    with host_errors():
        session = request.app.state.host_store.session(org, aid, session_id)
        return await request.app.state.host_runtime.request(
            org, aid, f"/session/{session['session_id']}/message", directory=session["directory"]
        )
