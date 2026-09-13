"""Host-bound peer configuration and peer bearer authentication."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from fesnyng_backend.host_routes import host_errors, require_binding
from fesnyng_backend.peer_configuration import PeerConfiguration
from fesnyng_backend.peer_configuration import require_peer as _require_peer

router = APIRouter(prefix="/organizations/{organization_id}", tags=["peers"])


def require_peer(request: Request, organization_id: str) -> str:
    """Authenticate an inbound peer bearer, never a host-management binding."""
    return _require_peer(request, organization_id)


@router.put("/peers")
def apply_configuration(request: Request, organization_id: UUID, body: PeerConfiguration):
    organization = str(organization_id)
    require_binding(request, organization)
    if str(body.organization_id) != organization:
        raise HTTPException(409, "Peer configuration identity mismatch")
    with host_errors():
        return request.app.state.peer_configuration.apply(body)


@router.get("/peers")
def peer_status(request: Request, organization_id: UUID):
    organization = str(organization_id)
    require_binding(request, organization)
    with host_errors():
        return request.app.state.peer_configuration.status(organization)
