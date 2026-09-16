"""Employees use resource operations only through their own authenticated capability."""

import asyncio
import json
from uuid import uuid4

import httpx

from fesnyng_backend.agent_host import create_app
from fesnyng_backend.host_models import HostAgentConfiguration
from fesnyng_backend.settings import ServiceSettings


def test_docker_tools_refuse_disabled_capability_and_foreign_source_thread(tmp_path):
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
        host.save_session(org, agent, f"thread_{index}", f"/workspace/thread_{index}", "Thread")
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
                    "clientInfo": {"name": "resource-test", "version": "1"},
                },
            )
            listed = await call(client, "tools/list", {})
            assert {tool["name"] for tool in listed["tools"]} >= {
                "docker_resources",
                "docker_discover",
                "docker_register",
                "docker_operate",
                "docker_associate",
                "service_list",
                "service_register",
                "service_associate",
                "service_unregister",
            }
            service = await call(
                client,
                "tools/call",
                {
                    "name": "service_register",
                    "arguments": {
                        "source_session_id": "thread_0",
                        "name": "Preview",
                        "endpoint_url": "http://127.0.0.1:8081",
                        "route": "custom",
                    },
                },
            )
            assert service.get("isError") is not True
            text = json.dumps(service)
            assert "network_reachability" in text
            assert "unverified" in text
            for session in ("thread_0", "thread_1"):
                result = await call(
                    client,
                    "tools/call",
                    {"name": "docker_discover", "arguments": {"source_session_id": session}},
                )
                assert result.get("isError") is True
                assert "/workspace/" not in json.dumps(result)
            spoof = await call(
                client,
                "tools/call",
                {
                    "name": "docker_operate",
                    "arguments": {
                        "source_session_id": "thread_0",
                        "resource_id": str(uuid4()),
                        "expected_revision": 1,
                        "action": "stop",
                        "agent_id": agents[1],
                    },
                },
            )
            assert spoof.get("isError") is True

    asyncio.run(exercise())
