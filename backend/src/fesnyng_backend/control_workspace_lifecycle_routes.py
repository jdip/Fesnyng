"""Member-authorized access to host-owned workspace lifecycle operations."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request, Response

from fesnyng_backend import auth, control_host_routes
from fesnyng_backend.host_models import Actor, NativeID, WorkspaceExpectation

router = APIRouter(prefix="/organizations/{organization_id}", tags=["workspaces"])


@router.get("/agents/{agent_id}/sessions/{session_id}/workspace")
async def inspect_workspace(
    request: Request, organization_id: UUID, agent_id: UUID, session_id: NativeID
):
    org, agent = str(organization_id), str(agent_id)
    auth.require_member(request, org)
    return await _forward(request, org, agent, session_id)


@router.post("/agents/{agent_id}/sessions/{session_id}/workspace/{action}")
async def change_workspace(
    request: Request,
    organization_id: UUID,
    agent_id: UUID,
    session_id: NativeID,
    action: Literal["remove", "discard", "replace"],
    body: WorkspaceExpectation,
):
    org, agent = str(organization_id), str(agent_id)
    user = auth.require_unsafe_request(request)
    auth.require_member(request, org)
    actor = Actor(kind="human", id=user.id, name=user.display_name)
    return await _forward(request, org, agent, session_id, action, body, actor)


async def _forward(
    request: Request,
    org: str,
    agent: str,
    session: str,
    action: str | None = None,
    body: WorkspaceExpectation | None = None,
    actor: Actor | None = None,
):
    client = control_host_routes.host_client(request)
    with control_host_routes.host_errors():
        assigned = client.agents.get_agent(org, agent)
        path = f"/agents/{agent}/sessions/{session}/workspace"
        reply = await client.raw_request(
            org,
            assigned["host_id"],
            f"{path}/{action}" if action else path,
            method="POST" if action else "GET",
            body=body.model_dump(mode="json") if body else None,
            headers={"X-Fesnyng-Actor": actor.model_dump_json()} if actor else None,
        )
    return Response(
        content=reply.content,
        status_code=reply.status_code,
        media_type=reply.content_type,
        headers={"Cache-Control": "no-store"},
    )
