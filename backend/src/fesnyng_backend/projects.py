"""Organization Projects and control-plane thread grouping."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import NoReturn
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import Field

from fesnyng_backend import auth
from fesnyng_backend.agent_models import Contract, Name
from fesnyng_backend.agent_storage import AgentStore
from fesnyng_backend.control_store import Project
from fesnyng_backend.host_client import HostResponse

router = APIRouter(prefix="/organizations/{organization_id}", tags=["projects"])

_NATIVE_ID = re.compile(r"^[A-Za-z0-9_-]{1,160}$")


class ProjectCreate(Contract):
    name: Name
    description: str | None = Field(default=None, max_length=4_000)
    target_repository_url: str | None = Field(default=None, max_length=2_000)
    default_checkout_branch: str | None = Field(default=None, max_length=500)


class ProjectUpdate(Contract):
    name: Name | None = None
    description: str | None = Field(default=None, max_length=4_000)
    target_repository_url: str | None = Field(default=None, max_length=2_000)
    default_checkout_branch: str | None = Field(default=None, max_length=500)


class ProjectResponse(Contract):
    id: str
    organization_id: str
    name: str
    description: str | None
    target_repository_url: str | None
    default_checkout_branch: str | None
    archived: bool


class ThreadProjectRequest(Contract):
    project_id: UUID | None


class ThreadProjectResponse(Contract):
    project_id: str | None


class ThreadProjectsResponse(Contract):
    threads: list[dict[str, str]]


@contextmanager
def _domain_errors() -> Iterator[None]:
    try:
        yield
    except LookupError as error:
        raise HTTPException(404, str(error)) from None
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


def _project_response(project: Project) -> ProjectResponse:
    return ProjectResponse(**project.__dict__)


def _agent(request: Request, organization_id: str, agent_id: str) -> None:
    try:
        AgentStore(auth.get_store(request)).get_agent(organization_id, agent_id)
    except LookupError:
        raise HTTPException(404, "Agent not found") from None


def _session(session_id: str) -> None:
    if not _NATIVE_ID.fullmatch(session_id):
        raise HTTPException(404, "Thread not found")


def require_active_project(request: Request, organization_id: str, project_id: str) -> Project:
    """Reject a missing or archived Project before a native thread is created."""

    project = auth.get_store(request).project_for(organization_id, project_id)
    if project.archived:
        raise ValueError("Project is not available for new thread grouping.")
    return project


async def update_host_project_provenance(
    request: Request,
    organization_id: str,
    agent_id: str,
    session_id: str,
    project_id: str | None,
) -> None:
    """Keep the host's peer-delivery provenance aligned with a human grouping choice."""

    # Keep control-plane startup one-directional: control_host_routes imports
    # this router, while the host client is only needed for a mutation.
    from fesnyng_backend.control_host_routes import host_client, host_errors

    client = host_client(request)
    with host_errors():
        assigned = client.agents.get_agent(organization_id, agent_id)
        await client.request(
            organization_id,
            assigned["host_id"],
            f"/agents/{agent_id}/sessions/{session_id}/project-provenance",
            method="PUT",
            body={"project_id": project_id},
        )


def reconcile_host_project_provenance(
    request: Request,
    organization_id: str,
    agent_id: str,
    session: dict[object, object],
) -> dict[object, object]:
    """Adopt one host-retained peer Project without surfacing host metadata to the browser."""

    projected = dict(session)
    project_id = projected.pop("fesnyng_project_id", None)
    projected.pop("project_provenance_initialized", None)
    session_id = projected.get("id") or projected.get("session_id")
    if not isinstance(session_id, str) or not _NATIVE_ID.fullmatch(session_id):
        return projected
    if not isinstance(project_id, str):
        return projected
    try:
        auth.get_store(request).attach_project_if_ungrouped(
            organization_id, agent_id, session_id, project_id
        )
    except (LookupError, ValueError):
        # A deleted Project is intentionally not resurrected from delayed host
        # provenance. Archived Projects remain valid navigation groupings.
        pass
    return projected


def reconcile_host_session_response(
    request: Request, organization_id: str, agent_id: str, reply: HostResponse
) -> HostResponse:
    """Reconcile and redact host-only Project provenance from a native inventory response."""

    if not 200 <= reply.status_code < 300:
        return reply
    try:
        sessions = json.loads(reply.content)
    except json.JSONDecodeError:
        return reply
    if not isinstance(sessions, list):
        return reply
    projected = [
        reconcile_host_project_provenance(request, organization_id, agent_id, session)
        if isinstance(session, dict)
        else session
        for session in sessions
    ]
    return HostResponse(
        reply.status_code,
        json.dumps(projected, separators=(",", ":")).encode(),
        reply.content_type,
    )


def project_id_for_native_session_create(body: object) -> str | None:
    """Remove the control-plane Project field before forwarding native input."""

    if not isinstance(body, dict) or "project_id" not in body:
        return None
    value = body.pop("project_id")
    if value is None:
        return None
    try:
        return str(UUID(str(value)))
    except ValueError:
        raise HTTPException(422, "Project id must be a UUID") from None


async def prepare_native_session_create(
    request: Request, organization_id: str, agent_id: str, body: object
) -> str | None:
    """Resolve a Project-owned checkout without exposing host workspace controls to browsers."""

    if not isinstance(body, dict):
        return None
    creation_id = body.get("creation_id")
    if creation_id is not None:
        try:
            creation_id = str(UUID(str(creation_id)))
        except ValueError:
            raise HTTPException(422, "Creation id must be a UUID") from None
        body["creation_id"] = creation_id
    if "repository_url" in body or "directory" in body or "requested_checkout_branch" in body:
        _preparation_failure(
            creation_id, "Native session creation does not accept a repository or directory"
        )
    try:
        project_id = project_id_for_native_session_create(body)
    except HTTPException as error:
        _preparation_failure(creation_id, str(error.detail))
    checkout_requested = "checkout_branch" in body
    checkout_branch = body.pop("checkout_branch", None)
    if checkout_requested and not isinstance(checkout_branch, str):
        _preparation_failure(creation_id, "Checkout branch must be a string")
    if checkout_requested:
        checkout_branch = checkout_branch.strip()
        if not checkout_branch:
            _preparation_failure(creation_id, "Checkout branch must not be empty")

    existing = (
        await workspace_creation(request, organization_id, agent_id, body["creation_id"])
        if creation_id is not None
        else None
    )
    if existing is not None:
        _apply_workspace_creation_snapshot(body, existing, project_id, checkout_branch)
        return project_id

    body["project_id"] = project_id
    body["requested_checkout_branch"] = checkout_branch
    if project_id is None:
        if checkout_requested:
            _preparation_failure(
                creation_id, "Checkout branch requires a repository-backed Project"
            )
        return None

    try:
        project = require_active_project(request, organization_id, project_id)
    except (LookupError, ValueError) as error:
        _preparation_failure(creation_id, str(error))
    if project.target_repository_url is None:
        if checkout_requested:
            _preparation_failure(
                creation_id, "Checkout branch requires a repository-backed Project"
            )
        return project_id

    if creation_id is None:
        raise HTTPException(422, "Repository-backed Project threads require a creation id")
    selected_branch = checkout_branch or project.default_checkout_branch
    if not isinstance(selected_branch, str) or not selected_branch.strip():
        _preparation_failure(creation_id, "Repository-backed Project requires a checkout branch")
    body["repository_url"] = project.target_repository_url
    body["checkout_branch"] = selected_branch.strip()
    return project_id


def _preparation_failure(creation_id: str | None, detail: str) -> NoReturn:
    """Only a known, retryable creation id can safely be replaced by the browser."""

    if creation_id is None:
        raise HTTPException(422, detail)
    raise HTTPException(
        503,
        {
            "code": "workspace_preparation_failed",
            "creation_id": creation_id,
            "detail": detail,
        },
    )


async def workspace_creation(
    request: Request, organization_id: str, agent_id: str, creation_id: str
) -> dict[str, object] | None:
    """Read the host-owned create receipt before current Project settings are consulted."""

    from fesnyng_backend.control_host_routes import host_client, host_errors

    client = host_client(request)
    try:
        with host_errors():
            assigned = client.agents.get_agent(organization_id, agent_id)
            creation = await client.request(
                organization_id,
                assigned["host_id"],
                f"/agents/{agent_id}/workspace-creations/{creation_id}",
            )
    except HTTPException as error:
        if error.status_code == 404:
            return None
        raise
    if not isinstance(creation, dict):
        raise HTTPException(503, "Agent host returned an invalid workspace creation receipt")
    return creation


def _apply_workspace_creation_snapshot(
    body: dict[object, object],
    creation: dict[str, object],
    project_id: str | None,
    checkout_branch: str | None,
) -> None:
    """Use an immutable host reservation only when the browser retried the same intent."""

    stored_project = creation.get("project_id")
    stored_override = creation.get("requested_checkout_branch")
    if stored_project != project_id or stored_override != checkout_branch:
        raise HTTPException(
            409, "Workspace creation retry does not match its original Project request"
        )
    repository_url = creation.get("repository_url")
    resolved_branch = creation.get("checkout_branch")
    if (repository_url is None) != (resolved_branch is None):
        raise HTTPException(503, "Agent host returned an invalid workspace creation receipt")
    if repository_url is not None and (
        not isinstance(repository_url, str) or not isinstance(resolved_branch, str)
    ):
        raise HTTPException(503, "Agent host returned an invalid workspace creation receipt")
    body["project_id"] = project_id
    body["requested_checkout_branch"] = checkout_branch
    if repository_url is not None:
        body["repository_url"] = repository_url
        body["checkout_branch"] = resolved_branch


async def attach_created_native_session_project(
    request: Request,
    organization_id: str,
    agent_id: str,
    project_id: str | None,
    reply: HostResponse,
    native_id_pattern: str,
) -> HostResponse:
    """Attach optional grouping without losing a verified native thread receipt."""

    if project_id is None or not 200 <= reply.status_code < 300:
        return reply
    try:
        created = json.loads(reply.content)
        session_id = created["id"] if isinstance(created, dict) else None
    except (KeyError, json.JSONDecodeError):
        session_id = None
    if not isinstance(session_id, str) or not re.fullmatch(native_id_pattern, session_id):
        raise HTTPException(503, "Agent host returned an invalid thread receipt")
    try:
        require_active_project(request, organization_id, project_id)
        await update_host_project_provenance(
            request, organization_id, agent_id, session_id, project_id
        )
        auth.get_store(request).set_thread_project(
            organization_id, agent_id, session_id, project_id
        )
    except (HTTPException, LookupError, ValueError) as error:
        return _grouping_warning(reply, organization_id, agent_id, session_id, project_id, error)
    return reply


def _grouping_warning(
    reply: HostResponse,
    organization_id: str,
    agent_id: str,
    session_id: str,
    project_id: str,
    error: HTTPException | LookupError | ValueError,
) -> HostResponse:
    created = json.loads(reply.content)
    assert isinstance(created, dict)
    detail = error.detail if isinstance(error, HTTPException) else str(error)
    created["fesnyng_project_grouping"] = {
        "state": "ungrouped",
        "requested_project_id": project_id,
        "retry_path": (
            f"/organizations/{organization_id}/agents/{agent_id}/sessions/{session_id}/project"
        ),
        "detail": detail,
    }
    return HostResponse(
        reply.status_code,
        json.dumps(created, separators=(",", ":")).encode(),
        reply.content_type,
    )


@router.get("/projects", response_model=list[ProjectResponse])
def list_projects(
    request: Request, organization_id: UUID, include_archived: bool = False
) -> list[ProjectResponse]:
    organization = str(organization_id)
    auth.require_member(request, organization)
    return [
        _project_response(project)
        for project in auth.get_store(request).list_projects(organization, include_archived)
    ]


@router.post("/projects", status_code=201, response_model=ProjectResponse)
def create_project(request: Request, organization_id: UUID, body: ProjectCreate) -> ProjectResponse:
    organization = str(organization_id)
    auth.require_unsafe_request(request)
    auth.require_member(request, organization)
    with _domain_errors():
        return _project_response(
            auth.get_store(request).create_project(organization, body.model_dump(mode="json"))
        )


@router.get("/projects/{project_id}", response_model=ProjectResponse)
def get_project(request: Request, organization_id: UUID, project_id: UUID) -> ProjectResponse:
    organization = str(organization_id)
    auth.require_member(request, organization)
    with _domain_errors():
        return _project_response(auth.get_store(request).project_for(organization, str(project_id)))


@router.patch("/projects/{project_id}", response_model=ProjectResponse)
def update_project(
    request: Request, organization_id: UUID, project_id: UUID, body: ProjectUpdate
) -> ProjectResponse:
    organization = str(organization_id)
    auth.require_unsafe_request(request)
    auth.require_member(request, organization)
    with _domain_errors():
        return _project_response(
            auth.get_store(request).update_project(
                organization, str(project_id), body.model_dump(mode="json", exclude_unset=True)
            )
        )


@router.post("/projects/{project_id}/archive", response_model=ProjectResponse)
def archive_project(request: Request, organization_id: UUID, project_id: UUID) -> ProjectResponse:
    organization = str(organization_id)
    auth.require_unsafe_request(request)
    auth.require_manager(request, organization)
    with _domain_errors():
        return _project_response(
            auth.get_store(request).set_project_archived(organization, str(project_id), True)
        )


@router.post("/projects/{project_id}/restore", response_model=ProjectResponse)
def restore_project(request: Request, organization_id: UUID, project_id: UUID) -> ProjectResponse:
    organization = str(organization_id)
    auth.require_unsafe_request(request)
    auth.require_manager(request, organization)
    with _domain_errors():
        return _project_response(
            auth.get_store(request).set_project_archived(organization, str(project_id), False)
        )


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(request: Request, organization_id: UUID, project_id: UUID) -> Response:
    organization = str(organization_id)
    auth.require_unsafe_request(request)
    auth.require_manager(request, organization)
    with _domain_errors():
        auth.get_store(request).delete_project(organization, str(project_id))
    return Response(status_code=204)


@router.get("/agents/{agent_id}/thread-projects", response_model=ThreadProjectsResponse)
def list_thread_projects(
    request: Request, organization_id: UUID, agent_id: UUID
) -> ThreadProjectsResponse:
    organization, agent = str(organization_id), str(agent_id)
    auth.require_member(request, organization)
    _agent(request, organization, agent)
    return ThreadProjectsResponse(
        threads=auth.get_store(request).list_thread_projects(organization, agent)
    )


@router.get(
    "/agents/{agent_id}/sessions/{session_id}/project", response_model=ThreadProjectResponse
)
def get_thread_project(
    request: Request, organization_id: UUID, agent_id: UUID, session_id: str
) -> ThreadProjectResponse:
    organization, agent = str(organization_id), str(agent_id)
    auth.require_member(request, organization)
    _agent(request, organization, agent)
    _session(session_id)
    return ThreadProjectResponse(
        project_id=auth.get_store(request).project_id_for_thread(organization, agent, session_id)
    )


@router.put(
    "/agents/{agent_id}/sessions/{session_id}/project", response_model=ThreadProjectResponse
)
async def set_thread_project(
    request: Request,
    organization_id: UUID,
    agent_id: UUID,
    session_id: str,
    body: ThreadProjectRequest,
) -> ThreadProjectResponse:
    organization, agent = str(organization_id), str(agent_id)
    auth.require_unsafe_request(request)
    auth.require_member(request, organization)
    _agent(request, organization, agent)
    _session(session_id)
    with _domain_errors():
        if body.project_id is not None:
            require_active_project(request, organization, str(body.project_id))
        await update_host_project_provenance(
            request,
            organization,
            agent,
            session_id,
            str(body.project_id) if body.project_id else None,
        )
        project_id = auth.get_store(request).set_thread_project(
            organization, agent, session_id, str(body.project_id) if body.project_id else None
        )
    return ThreadProjectResponse(project_id=project_id)
