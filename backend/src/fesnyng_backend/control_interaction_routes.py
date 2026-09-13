"""Human-authorized forwarding for host-owned native interactions and policy."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import Field

from fesnyng_backend import auth
from fesnyng_backend.agent_models import Contract, PermissionRule
from fesnyng_backend.control_host_routes import host_client, host_errors
from fesnyng_backend.host_models import Actor, NativeID

router = APIRouter(prefix="/organizations/{organization_id}", tags=["interactions"])


class QuestionReply(Contract):
    operation_id: UUID
    answers: list[list[str]]


class PermissionReply(Contract):
    operation_id: UUID
    reply: Literal["once", "reject"]


class PolicyWrite(Contract):
    expected_revision: int = Field(ge=0)
    rules: list[PermissionRule]


@router.get("/agents/{agent_id}/sessions/{session_id}/questions")
async def questions(request: Request, organization_id: UUID, agent_id: UUID, session_id: NativeID):
    organization, agent = str(organization_id), str(agent_id)
    auth.require_member(request, organization)
    return await _forward(request, organization, agent, f"/sessions/{session_id}/questions")


@router.post("/agents/{agent_id}/sessions/{session_id}/questions/{request_id}/reply")
async def reply_question(
    request: Request,
    organization_id: UUID,
    agent_id: UUID,
    session_id: NativeID,
    request_id: NativeID,
    body: QuestionReply,
):
    organization, agent = str(organization_id), str(agent_id)
    author = _authorized_author(request, organization)
    return await _forward(
        request,
        organization,
        agent,
        f"/sessions/{session_id}/questions/{request_id}/reply",
        method="POST",
        body={**body.model_dump(mode="json"), "author": author.model_dump(mode="json")},
    )


@router.get(
    "/agents/{agent_id}/sessions/{session_id}/questions/{request_id}/replies/{operation_id}"
)
async def question_reply_receipt(
    request: Request,
    organization_id: UUID,
    agent_id: UUID,
    session_id: NativeID,
    request_id: NativeID,
    operation_id: UUID,
):
    organization, agent = str(organization_id), str(agent_id)
    auth.require_member(request, organization)
    return await _forward(
        request,
        organization,
        agent,
        f"/sessions/{session_id}/questions/{request_id}/replies/{operation_id}",
    )


@router.get("/agents/{agent_id}/sessions/{session_id}/permissions")
async def permissions(
    request: Request, organization_id: UUID, agent_id: UUID, session_id: NativeID
):
    organization, agent = str(organization_id), str(agent_id)
    auth.require_member(request, organization)
    return await _forward(request, organization, agent, f"/sessions/{session_id}/permissions")


@router.post("/agents/{agent_id}/sessions/{session_id}/permissions/{request_id}/reply")
async def reply_permission(
    request: Request,
    organization_id: UUID,
    agent_id: UUID,
    session_id: NativeID,
    request_id: NativeID,
    body: PermissionReply,
):
    organization, agent = str(organization_id), str(agent_id)
    author = _authorized_author(request, organization)
    return await _forward(
        request,
        organization,
        agent,
        f"/sessions/{session_id}/permissions/{request_id}/reply",
        method="POST",
        body={**body.model_dump(mode="json"), "author": author.model_dump(mode="json")},
    )


@router.get(
    "/agents/{agent_id}/sessions/{session_id}/permissions/{request_id}/replies/{operation_id}"
)
async def permission_reply_receipt(
    request: Request,
    organization_id: UUID,
    agent_id: UUID,
    session_id: NativeID,
    request_id: NativeID,
    operation_id: UUID,
):
    organization, agent = str(organization_id), str(agent_id)
    auth.require_member(request, organization)
    return await _forward(
        request,
        organization,
        agent,
        f"/sessions/{session_id}/permissions/{request_id}/replies/{operation_id}",
    )


@router.get("/agents/{agent_id}/sessions/{session_id}/policy")
async def policy(request: Request, organization_id: UUID, agent_id: UUID, session_id: NativeID):
    organization, agent = str(organization_id), str(agent_id)
    auth.require_member(request, organization)
    return await _forward(request, organization, agent, f"/sessions/{session_id}/policy")


@router.put("/agents/{agent_id}/sessions/{session_id}/policy")
async def put_policy(
    request: Request,
    organization_id: UUID,
    agent_id: UUID,
    session_id: NativeID,
    body: PolicyWrite,
):
    organization, agent = str(organization_id), str(agent_id)
    author = _authorized_author(request, organization)
    return await _forward(
        request,
        organization,
        agent,
        f"/sessions/{session_id}/policy",
        method="PUT",
        body={**body.model_dump(mode="json"), "author": author.model_dump(mode="json")},
    )


async def _forward(
    request: Request,
    organization_id: str,
    agent_id: str,
    path: str,
    *,
    method: str = "GET",
    body: object = None,
):
    client = host_client(request)
    with host_errors():
        assigned = client.agents.get_agent(organization_id, agent_id)
        return await client.request(
            organization_id,
            assigned["host_id"],
            f"/agents/{agent_id}{path}",
            method=method,
            body=body,
        )


def _authorized_author(request: Request, organization_id: str) -> Actor:
    user = auth.require_unsafe_request(request)
    auth.require_member(request, organization_id)
    return Actor(kind="human", id=user.id, name=user.display_name)
