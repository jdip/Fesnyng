"""Control-plane application and status of directional peer topology."""

from uuid import UUID

from fastapi import APIRouter, Request

from fesnyng_backend import auth
from fesnyng_backend.control_host_routes import host_client
from fesnyng_backend.host_client import HostRejected, HostUnavailable
from fesnyng_backend.peer_configuration import ControlPeerConfigurationStore

router = APIRouter(prefix="/organizations/{organization_id}", tags=["peers"])


def peer_store(request: Request) -> ControlPeerConfigurationStore:
    return ControlPeerConfigurationStore(auth.get_store(request))


async def apply_peer_configuration(
    organization: str, configurations: ControlPeerConfigurationStore, client
) -> dict[str, object]:
    """Apply one frozen organization topology and retain truthful host status."""
    desired = configurations.desired(organization)
    envelopes = [
        (
            host["host_id"],
            configurations.configuration_for_host(organization, host["host_id"], desired),
        )
        for host in desired["hosts"]
    ]
    for host_id, configuration in envelopes:
        try:
            response = await client.request(
                organization,
                host_id,
                "/peers",
                method="PUT",
                body=configuration.model_dump(mode="json"),
            )
            applied = (
                isinstance(response, dict)
                and response.get("organization_id") == organization
                and response.get("host_id") == host_id
                and response.get("version") == configuration.version
            )
        except (HostRejected, HostUnavailable, LookupError):
            applied = False
        configurations.record_application(organization, host_id, configuration.version, applied)
    return configurations.status(organization)


@router.get("/peers")
def status(request: Request, organization_id: UUID):
    organization = str(organization_id)
    auth.require_member(request, organization)
    return peer_store(request).status(organization)


@router.post("/peers/apply")
async def apply(request: Request, organization_id: UUID):
    organization = str(organization_id)
    auth.require_unsafe_request(request)
    auth.require_manager(request, organization)
    configurations = peer_store(request)
    return await apply_peer_configuration(organization, configurations, host_client(request))
