"""Organization-bound HTTP access to host-owned native interactions and policy."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import Field

from fesnyng_backend.agent_models import Contract, PermissionRule
from fesnyng_backend.host_models import Actor, NativeID
from fesnyng_backend.host_routes import host_errors, require_binding

router = APIRouter(prefix="/organizations/{organization_id}", tags=["interactions"])


class QuestionReply(Contract):
    operation_id: UUID
    answers: list[list[str]]
    author: Actor


class PermissionReply(Contract):
    operation_id: UUID
    reply: Literal["once", "reject"]
    author: Actor


class PolicyWrite(Contract):
    expected_revision: int = Field(ge=0)
    rules: list[PermissionRule]
    author: Actor


@router.get("/agents/{agent_id}/sessions/{session_id}/questions")
async def questions(request: Request, organization_id: UUID, agent_id: UUID, session_id: NativeID):
    organization, agent = str(organization_id), str(agent_id)
    require_binding(request, organization)
    with host_errors():
        return await request.app.state.interactions.pending(
            organization, agent, session_id, "question"
        )


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
    require_binding(request, organization)
    with host_errors():
        return await request.app.state.interactions.reply(
            organization,
            agent,
            session_id,
            body.operation_id,
            request_id,
            "question",
            body.answers,
            body.author,
        )


@router.get(
    "/agents/{agent_id}/sessions/{session_id}/questions/{request_id}/replies/{operation_id}"
)
def question_reply_receipt(
    request: Request,
    organization_id: UUID,
    agent_id: UUID,
    session_id: NativeID,
    request_id: NativeID,
    operation_id: UUID,
):
    organization, agent = str(organization_id), str(agent_id)
    require_binding(request, organization)
    with host_errors():
        return request.app.state.interactions.get_reply(
            organization, agent, session_id, operation_id, request_id, "question"
        )


@router.get("/agents/{agent_id}/sessions/{session_id}/permissions")
async def permissions(
    request: Request, organization_id: UUID, agent_id: UUID, session_id: NativeID
):
    organization, agent = str(organization_id), str(agent_id)
    require_binding(request, organization)
    with host_errors():
        return await request.app.state.interactions.pending(
            organization, agent, session_id, "permission"
        )


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
    require_binding(request, organization)
    with host_errors():
        return await request.app.state.interactions.reply(
            organization,
            agent,
            session_id,
            body.operation_id,
            request_id,
            "permission",
            body.reply,
            body.author,
        )


@router.get(
    "/agents/{agent_id}/sessions/{session_id}/permissions/{request_id}/replies/{operation_id}"
)
def permission_reply_receipt(
    request: Request,
    organization_id: UUID,
    agent_id: UUID,
    session_id: NativeID,
    request_id: NativeID,
    operation_id: UUID,
):
    organization, agent = str(organization_id), str(agent_id)
    require_binding(request, organization)
    with host_errors():
        return request.app.state.interactions.get_reply(
            organization, agent, session_id, operation_id, request_id, "permission"
        )


@router.get("/agents/{agent_id}/sessions/{session_id}/policy")
def policy(request: Request, organization_id: UUID, agent_id: UUID, session_id: NativeID):
    organization, agent = str(organization_id), str(agent_id)
    require_binding(request, organization)
    with host_errors():
        return request.app.state.interactions.get_policy(organization, agent, session_id)


@router.put("/agents/{agent_id}/sessions/{session_id}/policy")
async def put_policy(
    request: Request,
    organization_id: UUID,
    agent_id: UUID,
    session_id: NativeID,
    body: PolicyWrite,
):
    organization, agent = str(organization_id), str(agent_id)
    require_binding(request, organization)
    with host_errors():
        request.app.state.interactions.put_policy(
            organization,
            agent,
            session_id,
            body.expected_revision,
            body.rules,
            body.author,
        )
        return await request.app.state.interactions.apply_policy(organization, agent, session_id)
