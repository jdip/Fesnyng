"""Member-authorized service links through an organization-bound host."""

import json
from uuid import UUID

from fastapi import APIRouter, Request, Response

from fesnyng_backend import auth, control_host_routes
from fesnyng_backend.host_docker_services import (
    ServiceExpectation,
    ServiceRegistration,
    ServiceUpdate,
)
from fesnyng_backend.host_models import Actor

router = APIRouter(
    prefix="/organizations/{organization_id}/hosts/{host_id}/docker/services", tags=["docker"]
)


async def _forward(
    request: Request,
    org: str,
    host: str,
    path: str = "",
    *,
    method: str = "GET",
    body: ServiceRegistration | ServiceUpdate | ServiceExpectation | None = None,
):
    auth.require_member(request, org)
    actor = None
    if method != "GET":
        user = auth.require_unsafe_request(request)
        actor = Actor(kind="human", id=user.id, name=user.display_name)
    client = control_host_routes.host_client(request)
    with control_host_routes.host_errors():
        client.agents.host_connection(org, host)
        if isinstance(body, ServiceRegistration):
            previous_threads: set[tuple[str, str]] = set()
            previous_projects: set[str] = set()
            previous_target: tuple[str, str] | None = None
            if isinstance(body, ServiceUpdate):
                previous = await client.raw_request(org, host, f"/docker/services{path}")
                if previous.status_code != 200:
                    from fesnyng_backend.host_client import HostRejected

                    raise HostRejected(previous.status_code)
                try:
                    item = json.loads(previous.content)
                    previous_threads = {
                        (thread["agent_id"], thread["session_id"])
                        for thread in item.get("threads", [])
                    }
                    previous_projects = set(item.get("project_ids", []))
                    previous_target = (item["target_kind"], item["target_id"])
                except (TypeError, ValueError, KeyError):
                    from fesnyng_backend.host_client import HostUnavailable

                    raise HostUnavailable("Agent host returned invalid service metadata") from None
            if body.target_kind == "employee" and previous_target != (
                body.target_kind,
                str(body.target_id),
            ):
                agent = client.agents.get_agent(org, str(body.target_id))
                if agent["host_id"] != host:
                    raise LookupError("Employee belongs to another host")
            for project_id in body.project_ids:
                if str(project_id) not in previous_projects:
                    auth.get_store(request).project_for(org, str(project_id))
            for thread in body.threads:
                if (str(thread.agent_id), thread.session_id) not in previous_threads:
                    agent = client.agents.get_agent(org, str(thread.agent_id))
                    if agent["host_id"] != host:
                        raise LookupError("Thread belongs to another host")
        reply = await client.raw_request(
            org,
            host,
            f"/docker/services{path}",
            method=method,
            body=body.model_dump(mode="json") if body else None,
            headers={"X-Fesnyng-Actor": actor.model_dump_json()} if actor else None,
        )
    return Response(
        content=reply.content,
        status_code=reply.status_code,
        media_type=reply.content_type,
        headers={"Cache-Control": "no-store"},
    )


@router.get("")
async def inventory(request: Request, organization_id: UUID, host_id: UUID):
    return await _forward(request, str(organization_id), str(host_id))


@router.post("")
async def register(
    request: Request, organization_id: UUID, host_id: UUID, body: ServiceRegistration
):
    return await _forward(request, str(organization_id), str(host_id), method="POST", body=body)


@router.get("/{service_id}")
async def inspect(request: Request, organization_id: UUID, host_id: UUID, service_id: UUID):
    return await _forward(request, str(organization_id), str(host_id), f"/{service_id}")


@router.put("/{service_id}")
async def update(
    request: Request,
    organization_id: UUID,
    host_id: UUID,
    service_id: UUID,
    body: ServiceUpdate,
):
    return await _forward(
        request, str(organization_id), str(host_id), f"/{service_id}", method="PUT", body=body
    )


@router.post("/{service_id}/unregister")
async def unregister(
    request: Request,
    organization_id: UUID,
    host_id: UUID,
    service_id: UUID,
    body: ServiceExpectation,
):
    return await _forward(
        request,
        str(organization_id),
        str(host_id),
        f"/{service_id}/unregister",
        method="POST",
        body=body,
    )
