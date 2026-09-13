import asyncio
import secrets
from uuid import uuid4

import httpx

from fesnyng_backend.agent_host import create_app
from fesnyng_backend.host_models import HostAgentConfiguration
from fesnyng_backend.settings import ServiceSettings


def test_host_exposes_cached_peer_configuration_and_native_collaboration_tools(tmp_path):
    app = create_app(
        ServiceSettings(
            service="agent-host",
            database_path=tmp_path / "host.db",
            state_directory=tmp_path / "state",
        )
    )
    org, agent = str(uuid4()), str(uuid4())
    binding = secrets.token_urlsafe(32)
    host = app.state.host_store
    host.bind_organization(org, binding)
    envelope = HostAgentConfiguration(
        host_id=host.instance_id, organization_id=org, agent_id=agent, version=1, name="Engineer"
    )
    host.stage_agent(envelope)
    host.mark_applied(envelope)
    agent_token = host.agent(org, agent)["agent_token"]

    async def check():
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://localhost"
            ) as client,
        ):
            body = {
                "organization_id": org,
                "host_id": str(host.instance_id),
                "version": 1,
                "agents": [
                    {"agent_id": agent, "host_id": str(host.instance_id), "name": "Engineer"}
                ],
                "peers": [],
            }
            response = await client.put(
                f"/organizations/{org}/peers",
                json=body,
                headers={"Authorization": f"Bearer {binding}"},
            )
            assert response.status_code == 200
            response = await client.post(
                "/mcp/",
                headers={"Authorization": f"Bearer {agent_token}"},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            )
            assert response.status_code == 200
            names = {tool["name"] for tool in response.json()["result"]["tools"]}
            assert {
                "memory_read",
                "discover_threads",
                "read_thread",
                "contribute_to_thread",
                "delegate",
                "collaboration_status",
            } <= names

    asyncio.run(check())
