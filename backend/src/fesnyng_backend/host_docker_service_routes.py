"""Bound host API for durable service metadata."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Request

from fesnyng_backend.host_docker_routes import require_actor
from fesnyng_backend.host_docker_services import (
    ServiceExpectation,
    ServiceRegistration,
    ServiceUpdate,
)
from fesnyng_backend.host_routes import host_errors, require_binding

router = APIRouter(prefix="/organizations/{organization_id}/docker/services", tags=["docker"])


@router.get("")
async def inventory(request: Request, organization_id: UUID):
    org = str(organization_id)
    require_binding(request, org)
    with host_errors():
        return {"services": await request.app.state.docker_services.inventory(org)}


@router.post("")
async def register(
    request: Request,
    organization_id: UUID,
    body: ServiceRegistration,
    actor: Annotated[str | None, Header(alias="X-Fesnyng-Actor")] = None,
):
    org = str(organization_id)
    require_binding(request, org)
    require_actor(actor)
    with host_errors():
        return await request.app.state.docker_services.register(org, body)


@router.get("/{service_id}")
async def inspect(request: Request, organization_id: UUID, service_id: UUID):
    org = str(organization_id)
    require_binding(request, org)
    with host_errors():
        return await request.app.state.docker_services.inspect(org, str(service_id))


@router.put("/{service_id}")
async def update(
    request: Request,
    organization_id: UUID,
    service_id: UUID,
    body: ServiceUpdate,
    actor: Annotated[str | None, Header(alias="X-Fesnyng-Actor")] = None,
):
    org = str(organization_id)
    require_binding(request, org)
    require_actor(actor)
    with host_errors():
        return await request.app.state.docker_services.update(org, str(service_id), body)


@router.post("/{service_id}/unregister")
async def unregister(
    request: Request,
    organization_id: UUID,
    service_id: UUID,
    body: ServiceExpectation,
    actor: Annotated[str | None, Header(alias="X-Fesnyng-Actor")] = None,
):
    org = str(organization_id)
    require_binding(request, org)
    require_actor(actor)
    with host_errors():
        return await request.app.state.docker_services.unregister(
            org, str(service_id), body.expected_revision
        )
