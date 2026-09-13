import asyncio
import secrets
from uuid import uuid4

from httpx import ASGITransport, AsyncClient

from fesnyng_backend.host_dispatch import Dispatcher, DispatchStore
from fesnyng_backend.host_mcp import create_memory_mcp, register_collaboration_tools
from fesnyng_backend.host_memory import MemoryStore
from fesnyng_backend.host_models import HostAgentConfiguration
from fesnyng_backend.host_store import HostStore
from fesnyng_backend.peer_configuration import PeerAgent, PeerConfiguration, PeerConfigurationStore
from fesnyng_backend.peer_delivery import PeerDeliveryService
from fesnyng_backend.peer_discovery import PeerDiscovery
from fesnyng_backend.settings import ServiceSettings


def test_collaboration_tools_discover_read_and_contribute_with_agent_attribution(tmp_path):
    server, app, system = _collaboration_mcp(tmp_path)

    async def check():
        async with (
            server.session_manager.run(),
            AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as client,
        ):
            initialized = await _request(
                client, system["token"], "initialize", _initialize_params()
            )
            assert initialized.status_code == 200
            listed = await _request(client, system["token"], "tools/list", {})
            assert {tool["name"] for tool in listed.json()["result"]["tools"]} >= {
                "memory_list",
                "memory_read",
                "memory_write",
                "discover_threads",
                "read_thread",
                "contribute_to_thread",
                "delegate",
                "collaboration_status",
            }

            discovered = await _request(
                client, system["token"], "tools/call", {"name": "discover_threads", "arguments": {}}
            )
            assert discovered.status_code == 200
            threads = discovered.json()["result"]["structuredContent"]["threads"]
            assert {thread["session_id"] for thread in threads} == {"ses_source", "ses_target"}

            read = await _request(
                client,
                system["token"],
                "tools/call",
                {
                    "name": "read_thread",
                    "arguments": {
                        "target_agent": system["target_agent"],
                        "session_id": "ses_target",
                    },
                },
            )
            assert read.status_code == 200
            assert (
                read.json()["result"]["structuredContent"]["result"][0]["info"]["sessionID"]
                == "ses_target"
            )

            delivery_id = str(uuid4())
            contributed = await _request(
                client,
                system["token"],
                "tools/call",
                {
                    "name": "contribute_to_thread",
                    "arguments": {
                        "id": delivery_id,
                        "source_session": "ses_source",
                        "target_agent": system["target_agent"],
                        "target_session": "ses_target",
                        "text": "Please incorporate this review.",
                        "mode": "steering",
                    },
                },
            )
            assert contributed.status_code == 200
            assert contributed.json()["result"]["structuredContent"]["state"] == "accepted"
            recorded = system["dispatch"].get(
                system["organization_id"], system["target_agent"], delivery_id
            )
            assert recorded["session_id"] == "ses_target"
            assert recorded["author"] == {
                "kind": "agent",
                "id": system["source_agent"],
                "name": "Source",
                "session_id": "ses_source",
            }

            status = await _request(
                client,
                system["token"],
                "tools/call",
                {"name": "collaboration_status", "arguments": {"delivery_id": delivery_id}},
            )
            assert status.json()["result"]["structuredContent"]["state"] == "accepted"

    asyncio.run(check())


def test_collaboration_tools_reject_a_foreign_claimed_source_session(tmp_path):
    server, app, system = _collaboration_mcp(tmp_path)

    async def check():
        async with (
            server.session_manager.run(),
            AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as client,
        ):
            rejected = await _request(
                client,
                system["token"],
                "tools/call",
                {
                    "name": "delegate",
                    "arguments": {
                        "id": str(uuid4()),
                        "source_session": "ses_target",
                        "target_agent": system["target_agent"],
                        "text": "This attribution must be rejected.",
                    },
                },
            )
            assert rejected.status_code == 200
            assert rejected.json()["result"]["isError"] is True
            assert system["dispatch"].pending() == []

    asyncio.run(check())


def test_discovery_tool_explains_invalid_workspace_names(tmp_path):
    server, app, system = _collaboration_mcp(tmp_path)

    async def check():
        async with (
            server.session_manager.run(),
            AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as client,
        ):
            response = await _request(
                client,
                system["token"],
                "tools/call",
                {"name": "discover_threads", "arguments": {"workspace": "/workspace/default"}},
            )
            result = response.json()["result"]
            assert result["isError"] is True
            text = " ".join(part.get("text", "") for part in result["content"])
            assert "workspace" in text
            assert "pattern" in text

    asyncio.run(check())


class Native:
    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = {}

    def lock(self, agent_id):
        return self._locks.setdefault(agent_id, asyncio.Lock())

    async def create_session(
        self, organization_id, agent_id, title, workspace, *, directory=None, metadata=None
    ):
        return {"id": "ses_delegated"}

    async def request(
        self, organization_id, agent_id, path, *, method="GET", body=None, directory=None
    ):
        if path == "/session/status":
            return {"ses_source": {"type": "idle"}, "ses_target": {"type": "idle"}}
        session_id = path.split("/")[2]
        return [
            {
                "info": {"id": f"msg_{session_id}", "sessionID": session_id},
                "parts": [{"type": "text", "text": f"History for {session_id}"}],
            }
        ]


def _collaboration_mcp(tmp_path):
    settings = ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "host.sqlite3",
        state_directory=tmp_path / "state",
    )
    host = HostStore(settings)
    host.initialize()
    organization_id = str(uuid4())
    host.bind_organization(organization_id, secrets.token_urlsafe(32))
    source_agent, target_agent = str(uuid4()), str(uuid4())
    for agent_id, name, session_id in (
        (source_agent, "Source", "ses_source"),
        (target_agent, "Target", "ses_target"),
    ):
        envelope = HostAgentConfiguration(
            host_id=host.instance_id,
            organization_id=organization_id,
            agent_id=agent_id,
            version=1,
            name=name,
        )
        host.stage_agent(envelope)
        host.mark_applied(envelope)
        host.save_session(
            organization_id,
            agent_id,
            session_id,
            f"/workspace/default/threads/{session_id}",
            f"{name} thread",
        )
    configuration = PeerConfigurationStore(host)
    configuration.initialize()
    configuration.apply(
        PeerConfiguration(
            organization_id=organization_id,
            host_id=host.instance_id,
            version=1,
            agents=[
                PeerAgent(agent_id=source_agent, name="Source", host_id=host.instance_id),
                PeerAgent(agent_id=target_agent, name="Target", host_id=host.instance_id),
            ],
        )
    )
    dispatch = DispatchStore(host)
    dispatch.initialize()
    native = Native()
    delivery = PeerDeliveryService(
        host, configuration, native, dispatch, Dispatcher(dispatch, native)
    )
    delivery.initialize()
    memory = MemoryStore(host)
    memory.initialize()
    server, app = create_memory_mcp(host, memory, "http://localhost:8001")
    register_collaboration_tools(server, host, PeerDiscovery(host, configuration, native), delivery)
    return (
        server,
        app,
        {
            "organization_id": organization_id,
            "source_agent": source_agent,
            "target_agent": target_agent,
            "token": host.agent(organization_id, source_agent)["agent_token"],
            "dispatch": dispatch,
        },
    )


async def _request(client, token, method, params):
    return await client.post(
        "/",
        headers={"Authorization": f"Bearer {token}"},
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )


def _initialize_params():
    return {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "peer-mcp-test", "version": "1.0"},
    }
