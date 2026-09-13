"""Human-authorized control-plane forwarding for host-owned dispatch receipts."""

from uuid import UUID

from fastapi import APIRouter, Request

from fesnyng_backend import auth
from fesnyng_backend.agent_models import Contract
from fesnyng_backend.control_host_routes import host_client, host_errors
from fesnyng_backend.host_dispatch import HostSubmission, Submission
from fesnyng_backend.host_dispatch_resolution import DispatchResolution, HostDispatchResolution
from fesnyng_backend.host_models import Actor, NativeID

router = APIRouter(prefix="/organizations/{organization_id}", tags=["dispatch"])


class StopRequest(Contract):
    id: UUID
    cancel_queued: bool = False


@router.post("/agents/{agent_id}/sessions/{session_id}/dispatches", status_code=202)
async def dispatch(
    request: Request,
    organization_id: UUID,
    agent_id: UUID,
    session_id: NativeID,
    body: Submission,
):
    organization, agent = str(organization_id), str(agent_id)
    author = _authorized_author(request, organization)
    submission = HostSubmission(**body.model_dump(), author=author)
    client = host_client(request)
    with host_errors():
        assigned = client.agents.get_agent(organization, agent)
        return await client.request(
            organization,
            assigned["host_id"],
            f"/agents/{agent}/sessions/{session_id}/dispatches",
            method="POST",
            body=submission.model_dump(mode="json"),
        )


@router.get("/agents/{agent_id}/sessions/{session_id}/dispatches")
async def list_dispatches(
    request: Request, organization_id: UUID, agent_id: UUID, session_id: NativeID
):
    organization, agent = str(organization_id), str(agent_id)
    auth.require_member(request, organization)
    client = host_client(request)
    with host_errors():
        assigned = client.agents.get_agent(organization, agent)
        return await client.request(
            organization, assigned["host_id"], f"/agents/{agent}/sessions/{session_id}/dispatches"
        )


@router.get("/agents/{agent_id}/sessions/{session_id}/dispatches/{delivery_id}")
async def dispatch_receipt(
    request: Request,
    organization_id: UUID,
    agent_id: UUID,
    session_id: NativeID,
    delivery_id: UUID,
):
    organization, agent = str(organization_id), str(agent_id)
    auth.require_member(request, organization)
    client = host_client(request)
    with host_errors():
        assigned = client.agents.get_agent(organization, agent)
        return await client.request(
            organization,
            assigned["host_id"],
            f"/agents/{agent}/sessions/{session_id}/dispatches/{delivery_id}",
        )


@router.post("/agents/{agent_id}/sessions/{session_id}/stop", status_code=202)
async def stop(
    request: Request,
    organization_id: UUID,
    agent_id: UUID,
    session_id: NativeID,
    body: StopRequest,
):
    organization, agent = str(organization_id), str(agent_id)
    author = _authorized_author(request, organization)
    client = host_client(request)
    with host_errors():
        assigned = client.agents.get_agent(organization, agent)
        return await client.request(
            organization,
            assigned["host_id"],
            f"/agents/{agent}/sessions/{session_id}/stop",
            method="POST",
            body={**body.model_dump(mode="json"), "author": author.model_dump(mode="json")},
        )


@router.post("/agents/{agent_id}/sessions/{session_id}/dispatches/{delivery_id}/reconcile")
async def reconcile_dispatch(
    request: Request,
    organization_id: UUID,
    agent_id: UUID,
    session_id: NativeID,
    delivery_id: UUID,
):
    organization, agent = str(organization_id), str(agent_id)
    _authorized_author(request, organization)
    client = host_client(request)
    with host_errors():
        assigned = client.agents.get_agent(organization, agent)
        return await client.request(
            organization,
            assigned["host_id"],
            f"/agents/{agent}/sessions/{session_id}/dispatches/{delivery_id}/reconcile",
            method="POST",
        )


@router.post("/agents/{agent_id}/sessions/{session_id}/dispatches/{delivery_id}/resolve")
async def resolve_dispatch(
    request: Request,
    organization_id: UUID,
    agent_id: UUID,
    session_id: NativeID,
    delivery_id: UUID,
    body: DispatchResolution,
):
    organization, agent = str(organization_id), str(agent_id)
    author = _authorized_author(request, organization)
    resolution = HostDispatchResolution(**body.model_dump(), author=author)
    client = host_client(request)
    with host_errors():
        assigned = client.agents.get_agent(organization, agent)
        return await client.request(
            organization,
            assigned["host_id"],
            f"/agents/{agent}/sessions/{session_id}/dispatches/{delivery_id}/resolve",
            method="POST",
            body=resolution.model_dump(mode="json"),
        )


def _authorized_author(request: Request, organization_id: str) -> Actor:
    user = auth.require_unsafe_request(request)
    auth.require_member(request, organization_id)
    return Actor(kind="human", id=user.id, name=user.display_name)
