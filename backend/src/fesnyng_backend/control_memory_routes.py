"""Human-authorized control-plane forwarding for host-owned agent memory."""

from uuid import UUID

from fastapi import APIRouter, Request

from fesnyng_backend import auth
from fesnyng_backend.agent_models import Slug
from fesnyng_backend.control_host_routes import host_client, host_errors
from fesnyng_backend.host_memory import HostMemoryWrite, MemoryWrite
from fesnyng_backend.host_models import Actor

router = APIRouter(prefix="/organizations/{organization_id}", tags=["memory"])


@router.get("/agents/{agent_id}/memory")
async def list_memory(request: Request, organization_id: UUID, agent_id: UUID):
    organization, agent = str(organization_id), str(agent_id)
    auth.require_member(request, organization)
    client = host_client(request)
    with host_errors():
        assigned = client.agents.get_agent(organization, agent)
        return await client.request(organization, assigned["host_id"], f"/agents/{agent}/memory")


@router.get("/agents/{agent_id}/memory/{key}")
async def get_memory(request: Request, organization_id: UUID, agent_id: UUID, key: Slug):
    organization, agent = str(organization_id), str(agent_id)
    auth.require_member(request, organization)
    client = host_client(request)
    with host_errors():
        assigned = client.agents.get_agent(organization, agent)
        return await client.request(
            organization, assigned["host_id"], f"/agents/{agent}/memory/{key}"
        )


@router.put("/agents/{agent_id}/memory")
async def put_memory(request: Request, organization_id: UUID, agent_id: UUID, body: MemoryWrite):
    organization, agent = str(organization_id), str(agent_id)
    user = auth.require_unsafe_request(request)
    auth.require_member(request, organization)
    write = HostMemoryWrite(
        **body.model_dump(), author=Actor(kind="human", id=user.id, name=user.display_name)
    )
    client = host_client(request)
    with host_errors():
        assigned = client.agents.get_agent(organization, agent)
        return await client.request(
            organization,
            assigned["host_id"],
            f"/agents/{agent}/memory",
            method="PUT",
            body=write.model_dump(mode="json"),
        )
