"""Organization-bound workspace inspection and explicit lifecycle actions."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import ValidationError

from fesnyng_backend.host_models import Actor, NativeID, WorkspaceExpectation
from fesnyng_backend.host_routes import host_errors, require_binding
from fesnyng_backend.host_workspace import Workspace

router = APIRouter(prefix="/organizations/{organization_id}", tags=["workspaces"])


def workspace_service(request: Request) -> Workspace:
    return Workspace(
        request.app.state.host_store,
        request.app.state.host_runtime,
        request.app.state.dispatch_store,
        request.app.state.interactions,
    )


@router.get("/agents/{agent_id}/sessions/{session_id}/workspace")
async def inspect_workspace(
    request: Request, organization_id: UUID, agent_id: UUID, session_id: NativeID
):
    org, agent = str(organization_id), str(agent_id)
    require_binding(request, org)
    with host_errors():
        return await workspace_service(request).workspace_inspection(org, agent, session_id)


@router.post("/agents/{agent_id}/sessions/{session_id}/workspace/{action}")
async def change_workspace(
    request: Request,
    organization_id: UUID,
    agent_id: UUID,
    session_id: NativeID,
    action: Literal["remove", "discard", "replace"],
    body: WorkspaceExpectation,
    actor: Annotated[str | None, Header(alias="X-Fesnyng-Actor")] = None,
):
    org, agent = str(organization_id), str(agent_id)
    require_binding(request, org)
    if not actor:
        raise HTTPException(400, "Trusted actor provenance is required")
    try:
        author = Actor.model_validate_json(actor)
    except ValidationError:
        raise HTTPException(400, "Trusted actor provenance is invalid") from None
    with host_errors():
        workspace = workspace_service(request)
        if action == "replace":
            return await workspace.replace_workspace(org, agent, session_id, body, author=author)
        return await workspace.remove_workspace(
            org, agent, session_id, body, discard=action == "discard", author=author
        )
