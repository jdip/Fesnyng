"""Native MCP tools for host-owned agent memory."""

from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

from fesnyng_backend.host_memory import MemoryStore
from fesnyng_backend.host_models import Actor
from fesnyng_backend.host_store import HostStore


class HostAgentTokenVerifier(TokenVerifier):
    """Verify an opaque host-issued agent key against durable host state."""

    def __init__(self, host_store: HostStore):
        self.host_store = host_store

    async def verify_token(self, token: str) -> AccessToken | None:
        identity = self.host_store.authenticate_agent(token)
        if identity is None:
            return None
        return AccessToken(
            token=token,
            client_id=identity["agent_id"],
            subject=identity["agent_id"],
            scopes=[],
        )


def create_memory_mcp(
    host_store: HostStore, memory_store: MemoryStore, host_base_url: str
) -> tuple[MCPServer, Starlette]:
    """Create the stateless, authenticated MCP app mounted by the agent host at ``/mcp``."""

    parsed = urlparse(host_base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Host MCP base URL must be an absolute HTTP(S) origin")

    server = MCPServer(
        "fesnyng-memory",
        instructions="Use these tools to inspect and edit only your durable Fesnyng memory.",
        auth=AuthSettings(
            issuer_url=host_base_url,
            resource_server_url=host_base_url,
            validate_token_resource=False,
        ),
        token_verifier=HostAgentTokenVerifier(host_store),
    )

    @server.tool(description="List this agent's durable memory entries.")
    def memory_list() -> list[dict[str, Any]]:
        identity = _authenticated_agent(host_store)
        return memory_store.list(identity["organization_id"], identity["agent_id"])

    @server.tool(description="Read one durable memory entry for this agent by key.")
    def memory_read(key: str) -> dict[str, Any] | None:
        identity = _authenticated_agent(host_store)
        return memory_store.get(identity["organization_id"], identity["agent_id"], key)

    @server.tool(
        description="Write one durable memory entry for this agent using its expected revision."
    )
    def memory_write(key: str, content: str, expected_revision: int) -> dict[str, Any]:
        identity = _authenticated_agent(host_store)
        return memory_store.put(
            identity["organization_id"],
            identity["agent_id"],
            key,
            content,
            expected_revision,
            Actor(kind="agent", id=UUID(identity["agent_id"]), name=identity["name"]),
        )

    app = server.streamable_http_app(
        streamable_http_path="/",
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=sorted(
                {parsed.netloc, "localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*"}
            ),
        ),
        host=parsed.hostname or "localhost",
    )
    return server, app


def _authenticated_agent(host_store: HostStore) -> dict[str, str]:
    access_token = get_access_token()
    identity = host_store.authenticate_agent(access_token.token) if access_token else None
    if identity is None:
        raise ToolError("Agent authentication required")
    return identity
