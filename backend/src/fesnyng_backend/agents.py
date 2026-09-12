"""Organization-authorized agent configuration and placement APIs."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from fesnyng_backend import auth
from fesnyng_backend.agent_models import (
    AgentCreate,
    AgentResponse,
    AgentUpdate,
    CredentialProfileCreate,
    HostResponse,
    PolicyResponse,
    PolicyUpdate,
    ProfileResponse,
)
from fesnyng_backend.agent_storage import AgentStore, ConfigurationConflict

router = APIRouter(prefix="/organizations/{organization_id}", tags=["agents"])


@contextmanager
def _domain_errors() -> Iterator[None]:
    try:
        yield
    except LookupError as error:
        raise HTTPException(404, str(error)) from None
    except ConfigurationConflict as error:
        raise HTTPException(409, str(error)) from None
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


def _store(request: Request) -> AgentStore:
    return AgentStore(auth.get_store(request))


def _manager(request: Request, organization_id: str) -> str:
    actor = auth.require_unsafe_request(request)
    auth.require_manager(request, organization_id)
    return actor.id


@router.get("/agents", response_model=list[AgentResponse])
def list_agents(request: Request, organization_id: str) -> list[dict[str, Any]]:
    auth.require_member(request, organization_id)
    return _store(request).list_agents(organization_id)


@router.get("/agents/{agent_id}", response_model=AgentResponse)
def get_agent(request: Request, organization_id: str, agent_id: str) -> dict[str, Any]:
    auth.require_member(request, organization_id)
    with _domain_errors():
        return _store(request).get_agent(organization_id, agent_id)


@router.post("/agents", status_code=201, response_model=AgentResponse)
def create_agent(request: Request, organization_id: str, body: AgentCreate) -> dict[str, Any]:
    actor_id = _manager(request, organization_id)
    with _domain_errors():
        return _store(request).create_agent(organization_id, actor_id, body.model_dump(mode="json"))


@router.patch("/agents/{agent_id}", response_model=AgentResponse)
def update_agent(
    request: Request, organization_id: str, agent_id: str, body: AgentUpdate
) -> dict[str, Any]:
    actor_id = _manager(request, organization_id)
    with _domain_errors():
        return _store(request).update_agent(
            organization_id,
            agent_id,
            actor_id,
            body.model_dump(mode="json", exclude_unset=True),
        )


@router.get("/hosts", response_model=list[HostResponse])
def list_hosts(request: Request, organization_id: str) -> list[dict[str, str]]:
    auth.require_member(request, organization_id)
    return _store(request).list_hosts(organization_id)


@router.get("/profiles", response_model=list[ProfileResponse])
def list_profiles(request: Request, organization_id: str) -> list[dict[str, str]]:
    auth.require_member(request, organization_id)
    return _store(request).list_profiles(organization_id)


@router.post("/profiles", status_code=201, response_model=ProfileResponse)
def create_profile(
    request: Request, organization_id: str, body: CredentialProfileCreate
) -> dict[str, str]:
    actor_id = _manager(request, organization_id)
    with _domain_errors():
        return _store(request).create_profile(
            organization_id, actor_id, body.model_dump(mode="json")
        )


@router.get("/policy", response_model=PolicyResponse)
def get_policy(request: Request, organization_id: str) -> dict[str, Any]:
    auth.require_member(request, organization_id)
    with _domain_errors():
        return _store(request).get_policy(organization_id)


@router.put("/policy", response_model=PolicyResponse)
def update_policy(request: Request, organization_id: str, body: PolicyUpdate) -> dict[str, Any]:
    actor_id = _manager(request, organization_id)
    with _domain_errors():
        return _store(request).update_policy(
            organization_id, actor_id, body.model_dump(mode="json")
        )
