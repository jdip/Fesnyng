"""Organization-bound HTTP receipts for durable native dispatch."""

from uuid import UUID

from fastapi import APIRouter, Request

from fesnyng_backend.agent_models import Contract
from fesnyng_backend.host_dispatch import HostSubmission
from fesnyng_backend.host_dispatch_resolution import HostDispatchResolution
from fesnyng_backend.host_models import Actor, NativeID
from fesnyng_backend.host_routes import host_errors, require_binding

router = APIRouter(prefix="/organizations/{organization_id}", tags=["dispatch"])


class StopRequest(Contract):
    id: UUID
    cancel_queued: bool = False
    author: Actor


@router.post("/agents/{agent_id}/sessions/{session_id}/dispatches", status_code=202)
def dispatch(
    request: Request,
    organization_id: UUID,
    agent_id: UUID,
    session_id: NativeID,
    body: HostSubmission,
):
    organization, agent = str(organization_id), str(agent_id)
    require_binding(request, organization)
    with host_errors():
        receipt = request.app.state.dispatch_store.enqueue(
            organization, agent, session_id, body, body.author
        )
    request.app.state.dispatcher.wake()
    return receipt


@router.get("/agents/{agent_id}/sessions/{session_id}/dispatches")
def list_dispatches(request: Request, organization_id: UUID, agent_id: UUID, session_id: NativeID):
    organization, agent = str(organization_id), str(agent_id)
    require_binding(request, organization)
    with host_errors():
        return request.app.state.dispatch_store.for_thread(organization, agent, session_id)


@router.get("/agents/{agent_id}/sessions/{session_id}/dispatches/{delivery_id}")
def dispatch_receipt(
    request: Request,
    organization_id: UUID,
    agent_id: UUID,
    session_id: NativeID,
    delivery_id: UUID,
):
    receipt = _receipt_for_thread(
        request, str(organization_id), str(agent_id), session_id, str(delivery_id)
    )
    return receipt


@router.post("/agents/{agent_id}/sessions/{session_id}/stop", status_code=202)
def stop(
    request: Request,
    organization_id: UUID,
    agent_id: UUID,
    session_id: NativeID,
    body: StopRequest,
):
    organization, agent = str(organization_id), str(agent_id)
    require_binding(request, organization)
    submission = HostSubmission(
        id=body.id,
        mode="stop",
        cancel_queued=body.cancel_queued,
        author=body.author,
    )
    with host_errors():
        receipt = request.app.state.dispatch_store.enqueue(
            organization, agent, session_id, submission, submission.author
        )
    request.app.state.dispatcher.wake()
    return receipt


@router.post("/agents/{agent_id}/sessions/{session_id}/dispatches/{delivery_id}/reconcile")
async def reconcile_dispatch(
    request: Request,
    organization_id: UUID,
    agent_id: UUID,
    session_id: NativeID,
    delivery_id: UUID,
):
    organization, agent = str(organization_id), str(agent_id)
    _receipt_for_thread(request, organization, agent, session_id, str(delivery_id))
    with host_errors():
        await request.app.state.dispatcher.reconcile_agent_effects(organization, agent)
        return request.app.state.dispatch_store.get(organization, agent, str(delivery_id))


@router.post("/agents/{agent_id}/sessions/{session_id}/dispatches/{delivery_id}/resolve")
async def resolve_dispatch(
    request: Request,
    organization_id: UUID,
    agent_id: UUID,
    session_id: NativeID,
    delivery_id: UUID,
    body: HostDispatchResolution,
):
    organization, agent = str(organization_id), str(agent_id)
    require_binding(request, organization)
    with host_errors():
        return await request.app.state.dispatch_resolution.resolve(
            organization, agent, session_id, delivery_id, body
        )


def _receipt_for_thread(
    request: Request, organization_id: str, agent_id: str, session_id: str, delivery_id: str
):
    require_binding(request, organization_id)
    with host_errors():
        receipt = request.app.state.dispatch_store.get(organization_id, agent_id, delivery_id)
        if receipt["session_id"] != session_id:
            raise LookupError("Delivery does not belong to thread")
    return receipt
