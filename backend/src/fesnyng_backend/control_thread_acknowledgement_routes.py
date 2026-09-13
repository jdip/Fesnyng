"""Personal read and handled receipts for durable thread outcomes."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from fesnyng_backend import auth
from fesnyng_backend.agent_models import Contract
from fesnyng_backend.agent_storage import AgentStore
from fesnyng_backend.control_host_routes import host_client, host_errors
from fesnyng_backend.host_models import NativeID

router = APIRouter(prefix="/organizations/{organization_id}/agents/{agent_id}", tags=["threads"])


class ThreadAcknowledgement(BaseModel):
    session_id: str
    delivery_id: str
    kind: Literal["read", "failure_handled"]
    outcome_id: str


class ThreadAcknowledgementsResponse(BaseModel):
    acknowledgements: list[ThreadAcknowledgement]


class ThreadAcknowledgementWrite(Contract):
    delivery_id: UUID
    kind: Literal["read", "failure_handled"]
    outcome_id: str = Field(min_length=1, max_length=200)


def _agent(request: Request, organization_id: str, agent_id: str) -> dict[str, object]:
    auth.require_member(request, organization_id)
    try:
        return AgentStore(auth.get_store(request)).get_agent(organization_id, agent_id)
    except LookupError:
        raise HTTPException(404, "Agent not found") from None


def _acknowledgements(
    request: Request, organization_id: str, user_id: str, agent_id: str
) -> ThreadAcknowledgementsResponse:
    return ThreadAcknowledgementsResponse(
        acknowledgements=auth.get_store(request).list_thread_acknowledgements(
            organization_id, user_id, agent_id
        )
    )


@router.get("/thread-acknowledgements")
def list_acknowledgements(
    request: Request, organization_id: UUID, agent_id: UUID
) -> ThreadAcknowledgementsResponse:
    organization, agent = str(organization_id), str(agent_id)
    user = auth.current_user(request)
    _agent(request, organization, agent)
    return _acknowledgements(request, organization, user.id, agent)


@router.post("/sessions/{session_id}/acknowledgements")
async def acknowledge(
    request: Request,
    organization_id: UUID,
    agent_id: UUID,
    session_id: NativeID,
    body: ThreadAcknowledgementWrite,
) -> ThreadAcknowledgementsResponse:
    organization, agent, session = str(organization_id), str(agent_id), str(session_id)
    user = auth.require_unsafe_request(request)
    assigned = _agent(request, organization, agent)
    delivery = str(body.delivery_id)
    client = host_client(request)
    with host_errors():
        receipt = await client.request(
            organization,
            str(assigned["host_id"]),
            f"/agents/{agent}/sessions/{session}/dispatches/{delivery}",
        )
    if (
        not isinstance(receipt, dict)
        or receipt.get("id") != delivery
        or receipt.get("session_id") != session
    ):
        raise HTTPException(404, "Delivery not found")
    expected = _outcome_id(receipt, body.kind, delivery)
    if expected is None or body.outcome_id != expected:
        raise HTTPException(409, "Delivery outcome is not eligible for acknowledgement")
    auth.get_store(request).acknowledge_thread(
        organization, user.id, agent, session, delivery, body.kind, expected
    )
    return _acknowledgements(request, organization, user.id, agent)


def _outcome_id(receipt: dict[str, object], kind: str, delivery_id: str) -> str | None:
    state, outcome = receipt.get("state"), receipt.get("outcome")
    if not isinstance(outcome, dict):
        outcome = {}
    outcome_kind = outcome.get("kind")
    if kind == "failure_handled":
        return (
            f"failed:{delivery_id}"
            if state == "failed" and outcome_kind != "operator_resolution"
            else None
        )
    if state != "completed":
        return None
    if outcome_kind == "native_run_completed" and isinstance(outcome.get("message_id"), str):
        return f"native:{outcome['message_id']}"
    if outcome_kind == "operator_resolution" and outcome.get("outcome") == "completed":
        operation_id = outcome.get("operation_id")
        if isinstance(operation_id, str):
            try:
                return f"resolution:{UUID(operation_id)}"
            except ValueError:
                return None
    return None
