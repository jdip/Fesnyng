"""Read-only collaboration endpoints authenticated by directional peer credentials."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from fesnyng_backend.agent_models import Contract
from fesnyng_backend.host_models import NativeID
from fesnyng_backend.host_routes import host_errors
from fesnyng_backend.peer_configuration import require_peer
from fesnyng_backend.peer_discovery import DiscoveryQuery

router = APIRouter(prefix="/organizations/{organization_id}/peer", tags=["peer discovery"])


class PeerDiscoveryRequest(Contract):
    source_agent: UUID
    query: DiscoveryQuery


class PeerReadRequest(Contract):
    source_agent: UUID
    agent_id: UUID
    session_id: NativeID


def require_source(request: Request, org: str, source_agent: UUID) -> None:
    source_host = require_peer(request, org)
    with host_errors():
        source = request.app.state.peer_configuration.agent(org, str(source_agent))
        if source["host_id"] != source_host:
            raise HTTPException(403, "Peer does not own the source agent")


@router.post("/discovery")
async def discover(request: Request, organization_id: UUID, body: PeerDiscoveryRequest):
    org = str(organization_id)
    require_source(request, org, body.source_agent)
    with host_errors():
        return await request.app.state.peer_discovery.local(org, body.query)


@router.post("/read")
async def read(request: Request, organization_id: UUID, body: PeerReadRequest):
    org = str(organization_id)
    require_source(request, org, body.source_agent)
    with host_errors():
        return await request.app.state.peer_discovery.read_local(
            org, str(body.agent_id), body.session_id
        )
