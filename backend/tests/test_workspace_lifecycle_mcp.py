"""Employees reach the same workspace lifecycle through authenticated native tools."""

import asyncio
import json
from uuid import uuid4

import httpx

from fesnyng_backend.agent_host import create_app
from fesnyng_backend.host_models import HostAgentConfiguration
from fesnyng_backend.settings import ServiceSettings


def test_workspace_tools_are_scoped_to_the_authenticated_employee(tmp_path):
    app = create_app(
        ServiceSettings(
            service="agent-host",
            database_path=tmp_path / "host.db",
            state_directory=tmp_path / "host",
        )
    )
    host = app.state.host_store
    org = str(uuid4())
    host.bind_organization(org, "a" * 32)
    agents = [str(uuid4()), str(uuid4())]
    for index, agent in enumerate(agents):
        host.stage_agent(
            HostAgentConfiguration(
                host_id=host.instance_id,
                organization_id=org,
                agent_id=agent,
                version=1,
                name=f"Engineer {index}",
            )
        )
        host.save_session(
            org, agent, f"thread_{index}", f"/workspace/thread_{index}", "Legacy thread"
        )
    token = host.agent(org, agents[0])["agent_token"]

    async def call(client, method, params):
        response = await client.post(
            "/mcp/",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        assert response.status_code == 200
        return response.json()["result"]

    async def exercise():
        async with (
            app.state.mcp_server.session_manager.run(),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://localhost"
            ) as client,
        ):
            await call(
                client,
                "initialize",
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "workspace-test", "version": "1"},
                },
            )
            listed = await call(client, "tools/list", {})
            assert {tool["name"] for tool in listed["tools"]} >= {
                "workspace_list",
                "workspace_inspect",
                "workspace_remove",
                "workspace_discard",
                "workspace_replace",
            }
            inspected = await call(
                client,
                "tools/call",
                {"name": "workspace_inspect", "arguments": {"session_id": "thread_0"}},
            )
            assert not inspected.get("isError")
            assert inspected["structuredContent"]["state"] == "legacy"
            assert inspected["structuredContent"]["cleanup"]["remove"]["available"] is False
            foreign = await call(
                client,
                "tools/call",
                {"name": "workspace_inspect", "arguments": {"session_id": "thread_1"}},
            )
            assert foreign.get("isError") is True
            assert "/workspace/thread_1" not in json.dumps(foreign)
            inventory = await call(
                client, "tools/call", {"name": "workspace_list", "arguments": {}}
            )
            assert not inventory.get("isError")
            assert [row["session_id"] for row in inventory["structuredContent"]["result"]] == [
                "thread_0"
            ]
            refused = await call(
                client,
                "tools/call",
                {
                    "name": "workspace_remove",
                    "arguments": {
                        "session_id": "thread_0",
                        "expected": {
                            "workspace_id": "thread_0",
                            "generation": 0,
                            "safety_digest": "a" * 64,
                        },
                    },
                },
            )
            assert refused.get("isError") is True
            assert "not host-owned" in json.dumps(refused)
            spoof = await call(
                client,
                "tools/call",
                {
                    "name": "workspace_remove",
                    "arguments": {
                        "session_id": "thread_0",
                        "expected": {
                            "workspace_id": "thread_0",
                            "generation": 0,
                            "safety_digest": "a" * 64,
                        },
                        "agent_id": agents[1],
                    },
                },
            )
            assert spoof.get("isError") is True

    asyncio.run(exercise())
