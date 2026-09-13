import asyncio
import secrets
from uuid import uuid4

from httpx import ASGITransport, AsyncClient

from fesnyng_backend.host_mcp import create_memory_mcp
from fesnyng_backend.host_memory import MemoryStore
from fesnyng_backend.host_models import HostAgentConfiguration
from fesnyng_backend.host_store import HostStore
from fesnyng_backend.settings import ServiceSettings


def test_memory_mcp_initializes_and_lists_native_memory_tools(tmp_path):
    server, app, identities = _memory_mcp(tmp_path)

    async def check():
        async with (
            server.session_manager.run(),
            AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as client,
        ):
            response = await _request(
                client, identities[0]["token"], "initialize", _initialize_params()
            )
            assert response.status_code == 200
            assert response.json()["result"]["serverInfo"]["name"] == "fesnyng-memory"

            response = await _request(client, identities[0]["token"], "tools/list", {})
            assert response.status_code == 200
            assert {tool["name"] for tool in response.json()["result"]["tools"]} == {
                "memory_list",
                "memory_read",
                "memory_write",
            }

    asyncio.run(check())


def test_memory_mcp_writes_and_reads_only_the_authenticated_agents_memory(tmp_path):
    server, app, identities = _memory_mcp(tmp_path, agent_count=2)
    first, second = identities

    async def check():
        async with (
            server.session_manager.run(),
            AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as client,
        ):
            written = await _request(
                client,
                first["token"],
                "tools/call",
                {
                    "name": "memory_write",
                    "arguments": {
                        "key": "preferences",
                        "content": "Use concise summaries.",
                        "expected_revision": 0,
                    },
                },
            )
            assert written.status_code == 200
            assert written.json()["result"]["structuredContent"]["revision"] == 1
            assert written.json()["result"]["structuredContent"]["author"] == {
                "kind": "agent",
                "id": first["agent_id"],
                "name": "Agent 1",
                "session_id": None,
            }

            own_memory = await _request(
                client,
                first["token"],
                "tools/call",
                {"name": "memory_read", "arguments": {"key": "preferences"}},
            )
            assert own_memory.status_code == 200
            assert (
                own_memory.json()["result"]["structuredContent"]["result"]["content"]
                == "Use concise summaries."
            )

            other_memory = await _request(
                client,
                second["token"],
                "tools/call",
                {"name": "memory_list", "arguments": {}},
            )
            assert other_memory.status_code == 200
            assert other_memory.json()["result"]["structuredContent"]["result"] == []

    asyncio.run(check())


def test_memory_mcp_rejects_missing_or_wrong_agent_keys(tmp_path):
    server, app, _ = _memory_mcp(tmp_path)

    async def check():
        async with (
            server.session_manager.run(),
            AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as client,
        ):
            missing = await client.post("/", json=_jsonrpc("tools/list", {}))
            assert missing.status_code == 401

            wrong = await _request(client, "not-a-host-agent-key", "tools/list", {})
            assert wrong.status_code == 401

    asyncio.run(check())


def _memory_mcp(tmp_path, agent_count=1):
    settings = ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "host.sqlite3",
        state_directory=tmp_path / "state",
    )
    host = HostStore(settings)
    host.initialize()
    organization_id = str(uuid4())
    host.bind_organization(organization_id, secrets.token_urlsafe(32))
    identities = []
    for number in range(agent_count):
        agent_id = str(uuid4())
        host.stage_agent(
            HostAgentConfiguration(
                host_id=host.instance_id,
                organization_id=organization_id,
                agent_id=agent_id,
                version=1,
                name=f"Agent {number + 1}",
            )
        )
        identities.append(
            {"agent_id": agent_id, "token": host.agent(organization_id, agent_id)["agent_token"]}
        )
    memory = MemoryStore(host)
    memory.initialize()
    server, app = create_memory_mcp(host, memory, "http://localhost:8001")

    return server, app, identities


async def _request(client, token, method, params):
    return await client.post(
        "/",
        headers={"Authorization": f"Bearer {token}"},
        json=_jsonrpc(method, params),
    )


def _jsonrpc(method, params):
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}


def _initialize_params():
    return {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "host-memory-test", "version": "1.0"},
    }
