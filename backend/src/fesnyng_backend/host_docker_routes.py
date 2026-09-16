"""Organization binding and trusted actor boundary for external Docker resources."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import ValidationError

from fesnyng_backend.host_docker_resources import (
    ResourceExpectation,
    ResourceRegistration,
    ResourceUpdate,
)
from fesnyng_backend.host_models import Actor
from fesnyng_backend.host_routes import host_errors, require_binding

router = APIRouter(prefix="/organizations/{organization_id}/docker", tags=["docker"])


def require_actor(actor: str | None) -> Actor:
    if not actor:
        raise HTTPException(400, "Trusted actor provenance is required")
    try:
        return Actor.model_validate_json(actor)
    except ValidationError:
        raise HTTPException(400, "Trusted actor provenance is invalid") from None


@router.get("")
async def inventory(request: Request, organization_id: UUID):
    org = str(organization_id)
    require_binding(request, org)
    with host_errors():
        return {
            "capability": (await request.app.state.docker_capability.status(org)).model_dump(
                mode="json"
            ),
            "resources": await request.app.state.docker_resources.inventory(org),
        }


@router.get("/capability")
async def capability(request: Request, organization_id: UUID, agent_id: UUID | None = None):
    org = str(organization_id)
    require_binding(request, org)
    with host_errors():
        if agent_id:
            request.app.state.host_store.agent(org, str(agent_id))
        return (
            await request.app.state.docker_capability.status(
                org, str(agent_id) if agent_id else None
            )
        ).model_dump(mode="json")


@router.get("/discovery")
async def discovery(request: Request, organization_id: UUID):
    org = str(organization_id)
    require_binding(request, org)
    with host_errors():
        return {"containers": await request.app.state.docker_resources.discover(org)}


@router.post("/resources")
async def register(
    request: Request,
    organization_id: UUID,
    body: ResourceRegistration,
    actor: Annotated[str | None, Header(alias="X-Fesnyng-Actor")] = None,
):
    org = str(organization_id)
    require_binding(request, org)
    require_actor(actor)
    with host_errors():
        return await request.app.state.docker_resources.register(org, body)


@router.get("/resources/{resource_id}")
async def inspect(request: Request, organization_id: UUID, resource_id: UUID):
    org = str(organization_id)
    require_binding(request, org)
    with host_errors():
        return await request.app.state.docker_resources.inspect(org, str(resource_id))


@router.put("/resources/{resource_id}")
async def update(
    request: Request,
    organization_id: UUID,
    resource_id: UUID,
    body: ResourceUpdate,
    actor: Annotated[str | None, Header(alias="X-Fesnyng-Actor")] = None,
):
    org = str(organization_id)
    require_binding(request, org)
    require_actor(actor)
    with host_errors():
        return await request.app.state.docker_resources.update(org, str(resource_id), body)


@router.post("/resources/{resource_id}/{action}")
async def operate(
    request: Request,
    organization_id: UUID,
    resource_id: UUID,
    action: Literal["start", "stop", "remove", "unregister"],
    body: ResourceExpectation,
    actor: Annotated[str | None, Header(alias="X-Fesnyng-Actor")] = None,
):
    org = str(organization_id)
    require_binding(request, org)
    require_actor(actor)
    with host_errors():
        return await request.app.state.docker_resources.operate(
            org, str(resource_id), body.expected_revision, action
        )
