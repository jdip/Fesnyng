"""Organization-bound host APIs for explicit agent memory."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from fesnyng_backend.agent_models import Slug
from fesnyng_backend.host_memory import HostMemoryWrite
from fesnyng_backend.host_routes import host_errors, require_binding

router = APIRouter(prefix="/organizations/{organization_id}", tags=["memory"])


@router.get("/agents/{agent_id}/memory")
def list_memory(request: Request, organization_id: UUID, agent_id: UUID):
    organization, agent = str(organization_id), str(agent_id)
    require_binding(request, organization)
    with host_errors():
        return request.app.state.host_memory.list(organization, agent)


@router.get("/agents/{agent_id}/memory/{key}")
def get_memory(request: Request, organization_id: UUID, agent_id: UUID, key: Slug):
    organization, agent = str(organization_id), str(agent_id)
    require_binding(request, organization)
    with host_errors():
        memory = request.app.state.host_memory.get(organization, agent, key)
    if memory is None:
        raise HTTPException(404, "Host memory not found")
    return memory


@router.put("/agents/{agent_id}/memory")
def put_memory(request: Request, organization_id: UUID, agent_id: UUID, body: HostMemoryWrite):
    organization, agent = str(organization_id), str(agent_id)
    require_binding(request, organization)
    with host_errors():
        return request.app.state.host_memory.put(
            organization,
            agent,
            body.key,
            body.content,
            body.expected_revision,
            body.author,
        )
