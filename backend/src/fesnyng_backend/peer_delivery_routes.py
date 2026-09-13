"""Host-bound peer acceptance endpoints."""

from uuid import UUID

from fastapi import APIRouter, Request

from fesnyng_backend.host_routes import host_errors
from fesnyng_backend.peer_configuration import require_peer
from fesnyng_backend.peer_delivery import PeerEnvelope

router = APIRouter(prefix="/organizations/{organization_id}", tags=["peer delivery"])


@router.post("/peers/deliveries", status_code=202)
async def receive(request: Request, organization_id: UUID, body: PeerEnvelope):
    organization = str(organization_id)
    source_host = require_peer(request, organization)
    with host_errors():
        if body.organization_id != organization_id:
            raise ValueError("Peer envelope organization mismatch")
        return await request.app.state.peer_delivery.receive(source_host, body)


@router.get("/peers/deliveries/{delivery_id}")
async def received(request: Request, organization_id: UUID, delivery_id: UUID):
    organization = str(organization_id)
    source_host = require_peer(request, organization)
    with host_errors():
        return request.app.state.peer_delivery.received(organization, source_host, str(delivery_id))
