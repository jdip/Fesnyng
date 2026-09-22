"""Agent-authorized organization management through its assigned host."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from fesnyng_backend import auth
from fesnyng_backend.agent_models import (
    AgentCreate,
    AgentResponse,
    AgentUpdate,
    DepartmentCreate,
    DepartmentResponse,
    DepartmentUpdate,
)
from fesnyng_backend.agent_storage import AgentStore, ConfigurationConflict
from fesnyng_backend.control_host_routes import host_client
from fesnyng_backend.control_peer_routes import apply_peer_configuration
from fesnyng_backend.departments import DepartmentStore
from fesnyng_backend.host_client import HostUnavailable
from fesnyng_backend.peer_configuration import ControlPeerConfigurationStore

human_router = APIRouter(prefix="/organizations/{organization_id}/agents", tags=["agents"])
agent_router = APIRouter(
    prefix="/agent-api/organizations/{organization_id}/agents/{caller_agent_id}",
    tags=["agent-management"],
)
POST_SAVE_RECONCILIATION_TIMEOUT_SECONDS = 20
AGENT_AUTHENTICATION_TIMEOUT_SECONDS = 5


class ManagementGrant(BaseModel):
    enabled: bool


@contextmanager
def _domain_errors() -> Iterator[None]:
    try:
        yield
    except LookupError as error:
        raise HTTPException(404, str(error)) from None
    except ConfigurationConflict as error:
        raise HTTPException(409, str(error)) from None
    except PermissionError:
        raise HTTPException(403, "Organization operation is not permitted") from None
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


def _store(request: Request) -> AgentStore:
    return AgentStore(auth.get_store(request))


def _department_store(request: Request) -> DepartmentStore:
    return DepartmentStore(auth.get_store(request))


@human_router.get("/{agent_id}/management", response_model=ManagementGrant)
def get_management(request: Request, organization_id: str, agent_id: str) -> ManagementGrant:
    auth.require_manager(request, organization_id)
    with _domain_errors():
        _store(request).get_agent(organization_id, agent_id)
        return ManagementGrant(
            enabled=_store(request).management_enabled(organization_id, agent_id)
        )


@human_router.put("/{agent_id}/management", response_model=ManagementGrant)
def update_management(
    request: Request, organization_id: str, agent_id: str, body: ManagementGrant
) -> ManagementGrant:
    auth.require_unsafe_request(request)
    auth.require_manager(request, organization_id)
    with _domain_errors():
        return ManagementGrant(
            enabled=_store(request).set_management_enabled(organization_id, agent_id, body.enabled)
        )


def _agent_token(request: Request) -> str:
    authorization = request.headers.get("Authorization")
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Agent authentication is required")
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(401, "Agent authentication is required")
    return token


async def _authorize_agent(
    request: Request, organization_id: str, caller_agent_id: str
) -> AgentStore:
    """Verify the caller only with its registered host, then read the live grant."""
    store = _store(request)
    try:
        caller = store.get_agent(organization_id, caller_agent_id)
    except LookupError:
        raise HTTPException(401, "Agent authentication failed") from None
    try:
        async with asyncio.timeout(AGENT_AUTHENTICATION_TIMEOUT_SECONDS):
            reply = await host_client(request).raw_request(
                organization_id,
                caller["host_id"],
                f"/agents/{caller_agent_id}/agent-authenticate",
                method="POST",
                headers={"X-Fesnyng-Agent-Token": _agent_token(request)},
            )
    except (HostUnavailable, LookupError) as error:
        raise HTTPException(503, str(error)) from None
    except TimeoutError:
        raise HTTPException(503, "Agent authentication service is unavailable") from None
    if not 200 <= reply.status_code < 300:
        raise HTTPException(401, "Agent authentication failed")
    if not store.management_enabled(organization_id, caller_agent_id):
        raise HTTPException(403, "Organization operation is not permitted")
    return store


async def _apply_or_pending(
    request: Request, store: AgentStore, organization_id: str, agent_id: str
) -> tuple[dict[str, Any], str | None]:
    try:
        return await host_client(request).apply(organization_id, agent_id), None
    except (HostUnavailable, LookupError) as error:
        # The desired version was committed before the host call.  A caller can
        # inspect the pending state and retry the dedicated apply operation.
        return store.get_agent(organization_id, agent_id), str(error)


async def _apply_peers(request: Request, organization_id: str) -> dict[str, object]:
    return await apply_peer_configuration(
        organization_id,
        ControlPeerConfigurationStore(auth.get_store(request)),
        host_client(request),
    )


def _peer_status(request: Request, organization_id: str) -> dict[str, object]:
    return ControlPeerConfigurationStore(auth.get_store(request)).status(organization_id)


async def _reconcile_agent(
    request: Request, store: AgentStore, organization_id: str, agent_id: str
) -> tuple[dict[str, Any], str | None, dict[str, object]]:
    try:
        async with asyncio.timeout(POST_SAVE_RECONCILIATION_TIMEOUT_SECONDS):
            agent, apply_error = await _apply_or_pending(request, store, organization_id, agent_id)
            return agent, apply_error, await _apply_peers(request, organization_id)
    except TimeoutError:
        return (
            store.get_agent(organization_id, agent_id),
            "Post-save application is pending after a timeout",
            _peer_status(request, organization_id),
        )


async def _reconcile_department(
    request: Request, organization_id: str
) -> tuple[dict[str, object], bool]:
    try:
        async with asyncio.timeout(POST_SAVE_RECONCILIATION_TIMEOUT_SECONDS):
            return await _apply_peers(request, organization_id), False
    except TimeoutError:
        return _peer_status(request, organization_id), True


def _peer_pending(peer_status: dict[str, object]) -> bool:
    hosts = peer_status.get("hosts")
    return isinstance(hosts, list) and any(
        isinstance(host, dict) and host.get("status") != "applied" for host in hosts
    )


def _agent_result(
    agent: dict[str, Any], apply_error: str | None, peer_status: dict[str, object]
) -> tuple[dict[str, Any], int]:
    if apply_error is None and not _peer_pending(peer_status):
        return agent, 200
    result: dict[str, Any] = {"agent": agent, "peer_configuration": peer_status}
    if apply_error is not None:
        result["apply_error"] = apply_error
    if _peer_pending(peer_status):
        result["peer_apply_error"] = "Peer host configuration is pending"
    return result, 202


def _department_result(
    department: dict[str, Any], peer_status: dict[str, object], timed_out: bool
) -> tuple[dict[str, Any], int]:
    if not timed_out and not _peer_pending(peer_status):
        return department, 200
    return {
        "department": department,
        "peer_configuration": peer_status,
        "peer_apply_error": (
            "Peer host configuration is pending after a timeout"
            if timed_out
            else "Peer host configuration is pending"
        ),
    }, 202


@agent_router.get("/agents", response_model=list[AgentResponse])
async def list_agents(
    request: Request, organization_id: str, caller_agent_id: str
) -> list[dict[str, Any]]:
    store = await _authorize_agent(request, organization_id, caller_agent_id)
    return store.list_agents(organization_id)


@agent_router.get("/agents/{agent_id}", response_model=AgentResponse)
async def get_agent(
    request: Request, organization_id: str, caller_agent_id: str, agent_id: str
) -> dict[str, Any]:
    store = await _authorize_agent(request, organization_id, caller_agent_id)
    with _domain_errors():
        return store.get_agent(organization_id, agent_id)


@agent_router.get("/departments", response_model=list[DepartmentResponse])
async def list_departments(
    request: Request, organization_id: str, caller_agent_id: str
) -> list[dict[str, Any]]:
    await _authorize_agent(request, organization_id, caller_agent_id)
    return _department_store(request).list_departments(organization_id)


@agent_router.get("/resources")
async def resources(request: Request, organization_id: str, caller_agent_id: str) -> dict[str, Any]:
    store = await _authorize_agent(request, organization_id, caller_agent_id)
    return {
        "hosts": store.list_hosts(organization_id),
        "profiles": store.list_profiles(organization_id),
    }


@agent_router.post("/agents", status_code=201)
async def create_agent(
    request: Request,
    organization_id: str,
    caller_agent_id: str,
    body: AgentCreate,
    response: Response,
) -> dict[str, Any]:
    store = await _authorize_agent(request, organization_id, caller_agent_id)
    with _domain_errors():
        created = store.create_agent_as_agent(
            organization_id, caller_agent_id, body.model_dump(mode="json")
        )
    applied, apply_error, peers = await _reconcile_agent(
        request, store, organization_id, created["id"]
    )
    result, status_code = _agent_result(applied, apply_error, peers)
    response.status_code = status_code if status_code != 200 else 201
    return result


@agent_router.patch("/agents/{agent_id}")
async def update_agent(
    request: Request,
    organization_id: str,
    caller_agent_id: str,
    agent_id: str,
    body: AgentUpdate,
    response: Response,
) -> dict[str, Any]:
    store = await _authorize_agent(request, organization_id, caller_agent_id)
    with _domain_errors():
        updated = store.update_agent_as_agent(
            organization_id,
            agent_id,
            caller_agent_id,
            body.model_dump(mode="json", exclude_unset=True),
        )
    applied, apply_error, peers = await _reconcile_agent(
        request, store, organization_id, updated["id"]
    )
    result, status_code = _agent_result(applied, apply_error, peers)
    response.status_code = status_code
    return result


@agent_router.post("/departments", status_code=201)
async def create_department(
    request: Request,
    organization_id: str,
    caller_agent_id: str,
    body: DepartmentCreate,
    response: Response,
) -> dict[str, Any]:
    await _authorize_agent(request, organization_id, caller_agent_id)
    with _domain_errors():
        created = _department_store(request).create_department_as_agent(
            organization_id, caller_agent_id, body.model_dump(mode="json")
        )
    peers, timed_out = await _reconcile_department(request, organization_id)
    result, status_code = _department_result(created, peers, timed_out)
    response.status_code = status_code if status_code != 200 else 201
    return result


@agent_router.patch("/departments/{department_id}")
async def update_department(
    request: Request,
    organization_id: str,
    caller_agent_id: str,
    department_id: str,
    body: DepartmentUpdate,
    response: Response,
) -> dict[str, Any]:
    await _authorize_agent(request, organization_id, caller_agent_id)
    with _domain_errors():
        updated = _department_store(request).update_department_as_agent(
            organization_id,
            department_id,
            caller_agent_id,
            body.model_dump(mode="json", exclude_unset=True),
        )
    peers, timed_out = await _reconcile_department(request, organization_id)
    result, status_code = _department_result(updated, peers, timed_out)
    response.status_code = status_code
    return result


@agent_router.post("/agents/{agent_id}/apply")
async def apply_agent(
    request: Request, organization_id: str, caller_agent_id: str, agent_id: str, response: Response
) -> dict[str, Any]:
    store = await _authorize_agent(request, organization_id, caller_agent_id)
    with _domain_errors():
        store.get_agent(organization_id, agent_id)
    applied, apply_error, peers = await _reconcile_agent(request, store, organization_id, agent_id)
    result, status_code = _agent_result(applied, apply_error, peers)
    response.status_code = status_code
    return result
