"""Organization-authorized agent configuration and placement APIs."""

import json
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
    HarnessSwitchRequest,
    HostResponse,
    PolicyResponse,
    PolicyUpdate,
    ProfileResponse,
)
from fesnyng_backend.agent_storage import AgentStore, ConfigurationConflict
from fesnyng_backend.control_host_routes import host_client
from fesnyng_backend.host_client import HostUnavailable

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


@router.post("/agents/{agent_id}/harness-switch", status_code=202)
async def switch_harness(
    request: Request, organization_id: str, agent_id: str, body: HarnessSwitchRequest
) -> dict[str, Any]:
    """Freeze verified old-harness history before selecting a replacement harness."""
    actor_id = _manager(request, organization_id)
    store = _store(request)
    with _domain_errors():
        intent = store.begin_harness_switch(
            organization_id,
            agent_id,
            actor_id,
            body.expected_version,
            body.target_runtime_type,
        )
    client = host_client(request)
    agent = store.get_agent(organization_id, agent_id)
    try:
        reply = await client.raw_request(
            organization_id,
            agent["host_id"],
            f"/agents/{agent_id}/harness-switch",
            method="POST",
            body=body.model_dump(mode="json"),
        )
    except HostUnavailable as error:
        raise HTTPException(503, str(error)) from None
    try:
        host_status = json.loads(reply.content)
    except json.JSONDecodeError:
        host_status = None
    if not 200 <= reply.status_code < 300:
        # A received error is not proof that the host did not commit.  Keep the
        # intent unless the host explicitly proves its source is writable again.
        status_code: int | None = None
        try:
            status_reply = await client.raw_request(
                organization_id, agent["host_id"], f"/agents/{agent_id}"
            )
            status_code = status_reply.status_code
            status = json.loads(status_reply.content)
        except (HostUnavailable, json.JSONDecodeError):
            status = None
        if (
            status_code is not None
            and 200 <= status_code < 300
            and isinstance(status, dict)
            and status.get("switch_state") is None
        ):
            store.abort_harness_switch(
                organization_id,
                agent_id,
                body.expected_version,
                body.target_runtime_type,
            )
        detail = (
            host_status.get("detail", "Agent host rejected the harness switch")
            if isinstance(host_status, dict)
            else "Agent host rejected the harness switch"
        )
        raise HTTPException(reply.status_code, detail)
    if not isinstance(host_status, dict) or any(
        host_status.get(key) != value
        for key, value in {
            "organization_id": organization_id,
            "agent_id": agent_id,
            "switch_state": "frozen",
            "switch_source_version": body.expected_version,
            "switch_target_runtime": body.target_runtime_type,
        }.items()
    ):
        raise HTTPException(503, "Host harness freeze acknowledgement is invalid")
    with _domain_errors():
        store.mark_harness_switch_frozen(
            organization_id,
            agent_id,
            body.expected_version,
            body.target_runtime_type,
        )
        selected = store.commit_harness_switch(
            organization_id,
            agent_id,
            actor_id,
            body.expected_version,
            body.target_runtime_type,
        )
    return {
        "agent": selected,
        "switch_state": "pending_apply",
        "message": "Historical threads are permanently frozen; apply the selected harness.",
        "intent": {"state": intent["state"], "target_runtime_type": body.target_runtime_type},
    }


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
