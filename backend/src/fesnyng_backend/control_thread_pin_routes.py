"""Personal, durable pin state for an organization's mapped agent threads."""

import re
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from fesnyng_backend import auth
from fesnyng_backend.agent_storage import AgentStore
from fesnyng_backend.control_host_routes import host_client, host_errors
from fesnyng_backend.host_client import HostUnavailable

router = APIRouter(
    prefix="/organizations/{organization_id}/agents/{agent_id}/thread-pins", tags=["threads"]
)

_NATIVE_ID = re.compile(r"^[A-Za-z0-9_-]{1,160}$")


class ThreadPinsResponse(BaseModel):
    session_ids: list[str]


def _agent(request: Request, organization_id: str, agent_id: str) -> dict[str, object]:
    auth.require_member(request, organization_id)
    try:
        return AgentStore(auth.get_store(request)).get_agent(organization_id, agent_id)
    except LookupError:
        raise HTTPException(404, "Agent not found") from None


def _pins(
    request: Request, organization_id: str, user_id: str, agent_id: str
) -> ThreadPinsResponse:
    return ThreadPinsResponse(
        session_ids=auth.get_store(request).list_thread_pins(organization_id, user_id, agent_id)
    )


@router.get("")
def list_pins(request: Request, organization_id: UUID, agent_id: UUID) -> ThreadPinsResponse:
    organization, agent = str(organization_id), str(agent_id)
    user = auth.current_user(request)
    _agent(request, organization, agent)
    return _pins(request, organization, user.id, agent)


@router.put("/{session_id}")
async def pin(
    request: Request, organization_id: UUID, agent_id: UUID, session_id: str
) -> ThreadPinsResponse:
    organization, agent = str(organization_id), str(agent_id)
    user = auth.require_unsafe_request(request)
    assigned = _agent(request, organization, agent)
    if not _NATIVE_ID.fullmatch(session_id):
        raise HTTPException(404, "Thread not found")
    client = host_client(request)
    with host_errors():
        session = await client.request(
            organization, str(assigned["host_id"]), f"/agents/{agent}/opencode/session/{session_id}"
        )
        if not isinstance(session, dict) or session.get("id") != session_id:
            raise HostUnavailable("Agent host returned an invalid thread receipt")
    auth.get_store(request).pin_thread(organization, user.id, agent, session_id)
    return _pins(request, organization, user.id, agent)


@router.delete("/{session_id}")
def unpin(
    request: Request, organization_id: UUID, agent_id: UUID, session_id: str
) -> ThreadPinsResponse:
    organization, agent = str(organization_id), str(agent_id)
    user = auth.require_unsafe_request(request)
    _agent(request, organization, agent)
    auth.get_store(request).unpin_thread(organization, user.id, agent, session_id)
    return _pins(request, organization, user.id, agent)
