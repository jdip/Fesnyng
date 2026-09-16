"""Member-authorized resource access through an organization-bound host."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request, Response

from fesnyng_backend import auth, control_host_routes
from fesnyng_backend.host_docker_resources import (
    ResourceAssociations,
    ResourceExpectation,
    ResourceRegistration,
    ResourceUpdate,
)
from fesnyng_backend.host_models import Actor

router = APIRouter(
    prefix="/organizations/{organization_id}/hosts/{host_id}/docker", tags=["docker"]
)


async def _forward(
    request: Request,
    org: str,
    host: str,
    path: str = "",
    *,
    method: str = "GET",
    body: ResourceRegistration | ResourceUpdate | ResourceExpectation | None = None,
    agent_id: UUID | None = None,
):
    auth.require_member(request, org)
    actor = None
    if method != "GET":
        user = auth.require_unsafe_request(request)
        actor = Actor(kind="human", id=user.id, name=user.display_name)
    client = control_host_routes.host_client(request)
    with control_host_routes.host_errors():
        # Resolve the host only through this organization's stored connection.
        client.agents.host_connection(org, host)
        if agent_id is not None:
            agent = client.agents.get_agent(org, str(agent_id))
            if agent["host_id"] != host:
                raise LookupError("Employee belongs to another host")
        if isinstance(body, ResourceAssociations):
            for project_id in body.project_ids:
                auth.get_store(request).project_for(org, str(project_id))
            for thread in body.threads:
                agent = client.agents.get_agent(org, str(thread.agent_id))
                if agent["host_id"] != host:
                    raise LookupError("Thread belongs to another host")
        reply = await client.raw_request(
            org,
            host,
            f"/docker{path}",
            method=method,
            body=body.model_dump(mode="json") if body else None,
            headers={"X-Fesnyng-Actor": actor.model_dump_json()} if actor else None,
            params={"agent_id": str(agent_id)} if agent_id else None,
        )
    return Response(
        content=reply.content,
        status_code=reply.status_code,
        media_type=reply.content_type,
        headers={"Cache-Control": "no-store"},
    )


@router.get("")
async def inventory(request: Request, organization_id: UUID, host_id: UUID):
    return await _forward(request, str(organization_id), str(host_id))


@router.get("/capability")
async def capability(
    request: Request, organization_id: UUID, host_id: UUID, agent_id: UUID | None = None
):
    return await _forward(
        request, str(organization_id), str(host_id), "/capability", agent_id=agent_id
    )


@router.get("/discovery")
async def discovery(request: Request, organization_id: UUID, host_id: UUID):
    return await _forward(request, str(organization_id), str(host_id), "/discovery")


@router.post("/resources")
async def register(
    request: Request, organization_id: UUID, host_id: UUID, body: ResourceRegistration
):
    return await _forward(
        request, str(organization_id), str(host_id), "/resources", method="POST", body=body
    )


@router.get("/resources/{resource_id}")
async def inspect(request: Request, organization_id: UUID, host_id: UUID, resource_id: UUID):
    return await _forward(request, str(organization_id), str(host_id), f"/resources/{resource_id}")


@router.put("/resources/{resource_id}")
async def update(
    request: Request, organization_id: UUID, host_id: UUID, resource_id: UUID, body: ResourceUpdate
):
    return await _forward(
        request,
        str(organization_id),
        str(host_id),
        f"/resources/{resource_id}",
        method="PUT",
        body=body,
    )


@router.post("/resources/{resource_id}/{action}")
async def operate(
    request: Request,
    organization_id: UUID,
    host_id: UUID,
    resource_id: UUID,
    action: Literal["start", "stop", "remove", "unregister"],
    body: ResourceExpectation,
):
    return await _forward(
        request,
        str(organization_id),
        str(host_id),
        f"/resources/{resource_id}/{action}",
        method="POST",
        body=body,
    )
