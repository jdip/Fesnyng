"""Human-authorized control-plane access to scoped host operations."""

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from fesnyng_backend import auth, projects
from fesnyng_backend.agent_lifecycle import HostLifecycleRequest, LifecycleRequest
from fesnyng_backend.agent_storage import AgentStore
from fesnyng_backend.host_client import HostClient, HostRejected, HostUnavailable
from fesnyng_backend.host_models import NativeID, SessionCreate

router = APIRouter(prefix="/organizations/{organization_id}", tags=["runtime"])


@contextmanager
def host_errors() -> Iterator[None]:
    try:
        yield
    except LookupError as error:
        raise HTTPException(404, str(error)) from None
    except HostRejected as error:
        raise HTTPException(error.status_code, str(error)) from None
    except HostUnavailable as error:
        raise HTTPException(503, str(error)) from None


def host_client(request: Request) -> HostClient:
    return HostClient(AgentStore(auth.get_store(request)))


@router.post("/agents/{agent_id}/apply")
async def apply(request: Request, organization_id: UUID, agent_id: UUID):
    org = str(organization_id)
    auth.require_unsafe_request(request)
    auth.require_manager(request, org)
    with host_errors():
        return await host_client(request).apply(org, str(agent_id))


@router.get("/agents/{agent_id}/runtime")
async def runtime_status(request: Request, organization_id: UUID, agent_id: UUID):
    org, aid = str(organization_id), str(agent_id)
    auth.require_member(request, org)
    client = host_client(request)
    with host_errors():
        agent = client.agents.get_agent(org, aid)
        return await client.request(org, agent["host_id"], f"/agents/{aid}")


@router.post("/agents/{agent_id}/lifecycle")
async def lifecycle(
    request: Request, organization_id: UUID, agent_id: UUID, body: LifecycleRequest
):
    org, aid = str(organization_id), str(agent_id)
    user = auth.require_unsafe_request(request)
    auth.require_manager(request, org)
    client = host_client(request)
    with host_errors():
        agent = client.agents.get_agent(org, aid)
        forwarded = HostLifecycleRequest(
            **body.model_dump(),
            author={"kind": "human", "id": user.id, "name": user.display_name},
        )
        return await client.request(
            org,
            agent["host_id"],
            f"/agents/{aid}/lifecycle",
            method="POST",
            body=forwarded.model_dump(mode="json"),
        )


@router.get("/agents/{agent_id}/sessions")
async def sessions(request: Request, organization_id: UUID, agent_id: UUID):
    org, aid = str(organization_id), str(agent_id)
    auth.require_member(request, org)
    client = host_client(request)
    with host_errors():
        agent = client.agents.get_agent(org, aid)
        inventory = await client.request(org, agent["host_id"], f"/agents/{aid}/sessions")
    if not isinstance(inventory, list):
        return inventory
    return [
        projects.reconcile_host_project_provenance(request, org, aid, session)
        if isinstance(session, dict)
        else session
        for session in inventory
    ]


@router.post("/agents/{agent_id}/sessions", status_code=201)
async def create_session(
    request: Request, organization_id: UUID, agent_id: UUID, body: SessionCreate
):
    org, aid = str(organization_id), str(agent_id)
    auth.require_unsafe_request(request)
    auth.require_member(request, org)
    client = host_client(request)
    with host_errors():
        agent = client.agents.get_agent(org, aid)
        return await client.request(
            org, agent["host_id"], f"/agents/{aid}/sessions", method="POST", body=body.model_dump()
        )


@router.get("/agents/{agent_id}/sessions/{session_id}/messages")
async def messages(request: Request, organization_id: UUID, agent_id: UUID, session_id: NativeID):
    org, aid = str(organization_id), str(agent_id)
    auth.require_member(request, org)
    client = host_client(request)
    with host_errors():
        agent = client.agents.get_agent(org, aid)
        return await client.request(
            org, agent["host_id"], f"/agents/{aid}/sessions/{session_id}/messages"
        )


def managed_profile(request: Request, organization_id: str, profile_id: str):
    auth.require_manager(request, organization_id)
    client = host_client(request)
    profile = next(
        (
            profile
            for profile in client.agents.list_profiles(organization_id)
            if profile["id"] == profile_id
        ),
        None,
    )
    if profile is None:
        raise HTTPException(404, "Credential profile not found")
    return client, profile


@router.get("/hosts/{host_id}/profiles/{profile_id}")
async def profile_status(request: Request, organization_id: UUID, host_id: UUID, profile_id: UUID):
    org, host, profile = str(organization_id), str(host_id), str(profile_id)
    client, _ = managed_profile(request, org, profile)
    with host_errors():
        return await client.request(org, host, f"/profiles/{profile}")


@router.post("/hosts/{host_id}/profiles/{profile_id}/login")
async def profile_login(request: Request, organization_id: UUID, host_id: UUID, profile_id: UUID):
    org, host, profile = str(organization_id), str(host_id), str(profile_id)
    auth.require_unsafe_request(request)
    client, metadata = managed_profile(request, org, profile)
    with host_errors():
        await client.request(
            org, host, f"/profiles/{profile}", method="PUT", body={"name": metadata["name"]}
        )
        return await client.request(org, host, f"/profiles/{profile}/login", method="POST")
