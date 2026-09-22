"""Agent-authenticated organization tools shared by both native harnesses."""

from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver.exceptions import ToolError

from fesnyng_backend.agent_models import (
    AgentCreate,
    AgentUpdate,
    DepartmentCreate,
    DepartmentUpdate,
)
from fesnyng_backend.host_store import HostStore


class OrganizationManagement:
    def __init__(self, host: HostStore, control_plane_url: str):
        parsed = urlsplit(control_plane_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Control-plane URL must be an HTTP(S) service URL")
        self.host = host
        self.url = control_plane_url.rstrip("/")
        self.transport: httpx.AsyncBaseTransport | None = None

    async def request(self, method: str, path: str, body: Any = None) -> Any:
        access = get_access_token()
        identity = self.host.authenticate_agent(access.token) if access else None
        if identity is None or access is None:
            raise ToolError("Agent authentication required")
        base = (
            f"{self.url}/agent-api/organizations/{identity['organization_id']}"
            f"/agents/{identity['agent_id']}"
        )
        try:
            async with httpx.AsyncClient(
                timeout=30, follow_redirects=False, transport=self.transport
            ) as client:
                response = await client.request(
                    method,
                    base + path,
                    headers={"Authorization": f"Bearer {access.token}"},
                    json=body,
                )
        except httpx.HTTPError:
            raise ToolError("Organization service is unavailable") from None
        if not response.is_success:
            if response.status_code in {401, 403}:
                raise ToolError("Organization operation is not permitted")
            try:
                detail = response.json().get("detail")
            except (ValueError, AttributeError):
                detail = None
            raise ToolError(detail if isinstance(detail, str) else "Organization operation failed")
        try:
            return response.json()
        except ValueError:
            raise ToolError("Organization service returned an invalid response") from None


def register_management_tools(server: MCPServer, management: OrganizationManagement) -> None:
    @server.tool(description="List agents and their desired configuration in your organization.")
    async def organization_list_agents() -> Any:
        return await management.request("GET", "/agents")

    @server.tool(description="Read an agent's configuration and version before updating it.")
    async def organization_get_agent(agent_id: UUID) -> Any:
        return await management.request("GET", f"/agents/{agent_id}")

    @server.tool(description="List departments, department heads, and their hierarchy.")
    async def organization_list_departments() -> Any:
        return await management.request("GET", "/departments")

    @server.tool(description="List registered hosts and credential profile metadata for setup.")
    async def organization_resources() -> Any:
        return await management.request("GET", "/resources")

    @server.tool(description="Create an agent in your organization using a registered host.")
    async def organization_create_agent(agent: AgentCreate) -> Any:
        return await management.request("POST", "/agents", agent.model_dump(mode="json"))

    @server.tool(
        description=(
            "Update an agent's identity, reporting, department or configuration, including core "
            "instructions, model and skills. Read the latest version first. Configuration replaces "
            "the complete configuration; preserve fields you are not changing. Changes apply at "
            "a safe boundary; a pending result means desired settings were saved."
        )
    )
    async def organization_update_agent(agent_id: UUID, update: AgentUpdate) -> Any:
        return await management.request(
            "PATCH", f"/agents/{agent_id}", update.model_dump(mode="json", exclude_unset=True)
        )

    @server.tool(description="Retry applying an agent's saved configuration at a safe boundary.")
    async def organization_apply_agent(agent_id: UUID) -> Any:
        return await management.request("POST", f"/agents/{agent_id}/apply")

    @server.tool(description="Create a department with an optional parent and department head.")
    async def organization_create_department(department: DepartmentCreate) -> Any:
        return await management.request("POST", "/departments", department.model_dump(mode="json"))

    @server.tool(description="Update a department's name, parent or department head.")
    async def organization_update_department(department_id: UUID, update: DepartmentUpdate) -> Any:
        return await management.request(
            "PATCH",
            f"/departments/{department_id}",
            update.model_dump(mode="json", exclude_unset=True),
        )
