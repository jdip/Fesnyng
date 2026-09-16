"""Authenticated MCP tools for host-owned employee capabilities."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import UUID

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

from fesnyng_backend.agent_models import Slug
from fesnyng_backend.host_memory import MemoryStore
from fesnyng_backend.host_models import Actor, NativeID, WorkspaceExpectation
from fesnyng_backend.host_runtime import RuntimeUnavailable
from fesnyng_backend.host_store import HostStore
from fesnyng_backend.host_workspace import Workspace
from fesnyng_backend.peer_delivery import PeerDeliveryService, PeerSend
from fesnyng_backend.peer_discovery import DiscoveryQuery, PeerDiscovery


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
        instructions="Use these tools for authorized Fesnyng memory, organization collaboration and your own thread workspaces.",
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


def register_collaboration_tools(
    server: MCPServer,
    host: HostStore,
    discovery: PeerDiscovery,
    delivery: PeerDeliveryService,
) -> None:
    """Register authenticated, attributable organization collaboration tools.

    Native MCP does not provide the current OpenCode thread ID.  Calls that
    create peer work therefore require a caller-supplied source session and
    validate that the authenticated agent owns it before any delivery is made.
    """

    @server.tool(
        description="Discover organization threads visible to this authenticated agent. Filter workspace by its assigned logical name, such as default."
    )
    async def discover_threads(
        agent_id: UUID | None = None,
        workspace: Slug | None = None,
        topic: str = "",
        active: bool | None = None,
    ) -> dict[str, Any]:
        identity = _authenticated_agent(host)
        return await discovery.discover(
            identity["organization_id"],
            identity["agent_id"],
            DiscoveryQuery(agent_id=agent_id, workspace=workspace, topic=topic, active=active),
        )

    @server.tool(description="Read an organization thread visible to this authenticated agent.")
    async def read_thread(target_agent: UUID, session_id: NativeID) -> list[dict[str, Any]]:
        identity = _authenticated_agent(host)
        return await discovery.read(
            identity["organization_id"], identity["agent_id"], str(target_agent), session_id
        )

    @server.tool(
        description=(
            "Contribute to an existing thread. source_session is caller-supplied attribution and must "
            "belong to this authenticated agent. Reuse the same stable id for an identical retry."
        )
    )
    async def contribute_to_thread(
        id: UUID,
        source_session: NativeID,
        target_agent: UUID,
        target_session: NativeID,
        text: str,
        mode: Literal["queued", "steering"] = "queued",
        origin_id: UUID | None = None,
    ) -> dict[str, Any]:
        identity = _authenticated_agent(host)
        _owned_source_session(host, identity, source_session)
        return await delivery.send(
            identity["organization_id"],
            identity["agent_id"],
            source_session,
            PeerSend(
                id=id,
                target_agent=target_agent,
                target_session=target_session,
                text=text,
                mode=mode,
                origin_id=origin_id,
            ),
        )

    @server.tool(
        description=(
            "Delegate new peer work. source_session is caller-supplied attribution and must belong "
            "to this authenticated agent. Reuse the same stable id for an identical retry."
        )
    )
    async def delegate(
        id: UUID,
        source_session: NativeID,
        target_agent: UUID,
        text: str,
        workspace: str = "default",
        title: str = "Peer collaboration",
        origin_id: UUID | None = None,
    ) -> dict[str, Any]:
        identity = _authenticated_agent(host)
        _owned_source_session(host, identity, source_session)
        return await delivery.send(
            identity["organization_id"],
            identity["agent_id"],
            source_session,
            PeerSend(
                id=id,
                target_agent=target_agent,
                text=text,
                workspace=workspace,
                title=title,
                origin_id=origin_id,
            ),
        )

    @server.tool(description="Read this authenticated agent's peer delivery receipt by stable id.")
    def collaboration_status(delivery_id: UUID) -> dict[str, Any]:
        identity = _authenticated_agent(host)
        return delivery.status(identity["organization_id"], identity["agent_id"], str(delivery_id))


def _authenticated_agent(host_store: HostStore) -> dict[str, str]:
    access_token = get_access_token()
    identity = host_store.authenticate_agent(access_token.token) if access_token else None
    if identity is None:
        raise ToolError("Agent authentication required")
    return identity


def _owned_source_session(host: HostStore, identity: dict[str, str], session_id: str) -> None:
    try:
        host.require_writable(identity["organization_id"], identity["agent_id"], session_id)
    except LookupError:
        raise ToolError("Source thread does not belong to the authenticated agent") from None
    except ValueError as error:
        raise ToolError(str(error)) from None


def register_workspace_tools(server: MCPServer, host: HostStore, workspace: Workspace) -> None:
    """Expose the host lifecycle to the authenticated employee's own mapped threads."""

    @server.tool(
        description="List your mapped thread workspaces, including archived threads. This does not clean up files or services."
    )
    async def workspace_list() -> list[dict[str, Any]]:
        with _workspace_tool_errors():
            identity = _authenticated_agent(host)
            org, agent = identity["organization_id"], identity["agent_id"]
            return [
                {
                    "session_id": row["session_id"],
                    "title": row["title"],
                    **await workspace.workspace_inspection(org, agent, row["session_id"]),
                }
                for row in host.sessions(org, agent, archived=None)
            ]

    @server.tool(
        description="Inspect one of your thread workspaces and its current cleanup safeguards. Retain its workspace_id, generation and safety_digest for an explicit action."
    )
    async def workspace_inspect(session_id: NativeID) -> dict[str, Any]:
        with _workspace_tool_errors():
            identity = _authenticated_agent(host)
            return await workspace.workspace_inspection(
                identity["organization_id"], identity["agent_id"], session_id
            )

    async def change(
        session_id: str, expected: WorkspaceExpectation, action: str
    ) -> dict[str, Any]:
        with _workspace_tool_errors():
            identity = _authenticated_agent(host)
            org, agent = identity["organization_id"], identity["agent_id"]
            # The token establishes ownership; callers cannot supply another employee or path.
            host.session(org, agent, session_id)
            author = Actor(kind="agent", id=UUID(agent), name=identity["name"])
            if action == "replace":
                return await workspace.replace_workspace(
                    org, agent, session_id, expected, author=author
                )
            return await workspace.remove_workspace(
                org, agent, session_id, expected, discard=action == "discard", author=author
            )

    @server.tool(
        description="Remove a verified safe workspace using its exact current inspection. Refuses active work, dirty/untracked files, unpushed commits or unverified safety. Retains conversation history; never tears down Docker stacks."
    )
    async def workspace_remove(
        session_id: NativeID, expected: WorkspaceExpectation
    ) -> dict[str, Any]:
        return await change(session_id, expected, "remove")

    @server.tool(
        description="Explicitly discard the known files/changes shown in this exact workspace inspection. Still refuses active work, ownership/history uncertainty and stale evidence. Use only when your workflow authorizes the displayed loss."
    )
    async def workspace_discard(
        session_id: NativeID, expected: WorkspaceExpectation
    ) -> dict[str, Any]:
        return await change(session_id, expected, "discard")

    @server.tool(
        description="Explicitly prepare a removed workspace at its existing path for the same native thread. Does not create a new conversation or unfreeze a permanently frozen thread."
    )
    async def workspace_replace(
        session_id: NativeID, expected: WorkspaceExpectation
    ) -> dict[str, Any]:
        return await change(session_id, expected, "replace")


@contextmanager
def _workspace_tool_errors() -> Iterator[None]:
    """Return useful expected refusals without disclosing another employee's state."""
    try:
        yield
    except LookupError:
        raise ToolError("Thread workspace not found") from None
    except PermissionError:
        raise ToolError("Workspace operation not permitted") from None
    except (ValueError, RuntimeUnavailable) as error:
        raise ToolError(str(error)) from None
