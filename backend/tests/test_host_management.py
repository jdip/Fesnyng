import asyncio
import secrets
from uuid import uuid4

from httpx import ASGITransport, AsyncClient

from fesnyng_backend.agent_host import create_app
from fesnyng_backend.host_models import HostAgentConfiguration
from fesnyng_backend.settings import ServiceSettings


def test_agent_identity_verification_requires_binding_and_exact_caller(tmp_path):
    app = create_app(
        ServiceSettings(
            service="agent-host",
            database_path=tmp_path / "host.db",
            state_directory=tmp_path / "state",
        )
    )
    store = app.state.host_store
    org, other, agent, second = (str(uuid4()) for _ in range(4))
    binding = secrets.token_urlsafe(32)
    store.bind_organization(org, binding)
    for aid in (agent, second):
        store.stage_agent(
            HostAgentConfiguration(
                host_id=store.instance_id,
                organization_id=org,
                agent_id=aid,
                version=1,
                name="Employee",
            )
        )
    token = store.agent(org, agent)["agent_token"]

    async def check():
        async with AsyncClient(transport=ASGITransport(app), base_url="http://host") as client:
            path = f"/organizations/{org}/agents/{agent}/agent-authenticate"
            assert (
                await client.post(path, headers={"X-Fesnyng-Agent-Token": token})
            ).status_code == 401
            client.headers["Authorization"] = f"Bearer {binding}"
            assert (await client.post(path)).status_code == 403
            assert (
                await client.post(path, headers={"X-Fesnyng-Agent-Token": "wrong"})
            ).status_code == 403
            assert (await client.post(path, headers={"X-Fesnyng-Agent-Token": token})).json() == {
                "authenticated": True
            }
            assert (
                await client.post(
                    f"/organizations/{org}/agents/{second}/agent-authenticate",
                    headers={"X-Fesnyng-Agent-Token": token},
                )
            ).status_code == 403
            assert (
                await client.post(
                    f"/organizations/{other}/agents/{agent}/agent-authenticate",
                    headers={"X-Fesnyng-Agent-Token": token},
                )
            ).status_code == 403

    asyncio.run(check())


def test_management_tools_bind_caller_and_hide_grant_fields(tmp_path):
    import json

    from httpx import MockTransport, Response

    from test_host_mcp import _request

    app = create_app(
        ServiceSettings(
            service="agent-host",
            database_path=tmp_path / "host.db",
            state_directory=tmp_path / "state",
        )
    )
    store = app.state.host_store
    org, agent, target = (str(uuid4()) for _ in range(3))
    store.bind_organization(org, secrets.token_urlsafe(32))
    store.stage_agent(
        HostAgentConfiguration(
            host_id=store.instance_id,
            organization_id=org,
            agent_id=agent,
            version=1,
            name="Manager",
        )
    )
    token = store.agent(org, agent)["agent_token"]
    requests = []

    def control(request):
        requests.append(request)
        return Response(
            200, json={"id": target, "configuration": {"instructions": "Updated core instructions"}}
        )

    app.state.organization_management.transport = MockTransport(control)

    async def check():
        async with (
            app.state.mcp_server.session_manager.run(),
            AsyncClient(transport=ASGITransport(app), base_url="http://localhost/mcp") as client,
        ):
            listed = await _request(client, token, "tools/list", {})
            tools = [
                tool
                for tool in listed.json()["result"]["tools"]
                if tool["name"].startswith("organization_")
            ]
            assert len(tools) == 9
            schemas = json.dumps([tool["inputSchema"] for tool in tools])
            assert "management" not in schemas
            assert "organization_id" not in schemas
            updated = await _request(
                client,
                token,
                "tools/call",
                {
                    "name": "organization_update_agent",
                    "arguments": {
                        "agent_id": target,
                        "update": {
                            "expected_version": 1,
                            "configuration": {
                                "runtime_type": "codex",
                                "instructions": "Updated core instructions",
                                "reasoning_effort": "high",
                            },
                        },
                    },
                },
            )
            assert not updated.json()["result"].get("isError")
            assert (
                requests[-1].url.path
                == f"/api/agent-api/organizations/{org}/agents/{agent}/agents/{target}"
            )
            assert requests[-1].headers["Authorization"] == f"Bearer {token}"
            assert json.loads(requests[-1].content)["configuration"]["reasoning_effort"] == "high"
            assert (
                json.loads(requests[-1].content)["configuration"]["instructions"]
                == "Updated core instructions"
            )
            denied = await _request(
                client,
                token,
                "tools/call",
                {
                    "name": "organization_update_agent",
                    "arguments": {
                        "agent_id": target,
                        "update": {"expected_version": 1, "management": True},
                    },
                },
            )
            assert denied.json()["result"]["isError"]
            assert len(requests) == 1

    asyncio.run(check())


def test_management_tools_fail_closed_without_following_redirects(tmp_path):
    from httpx import MockTransport, Response

    from test_host_mcp import _request

    app = create_app(
        ServiceSettings(
            service="agent-host",
            database_path=tmp_path / "host.db",
            state_directory=tmp_path / "state",
        )
    )
    store = app.state.host_store
    org, agent = str(uuid4()), str(uuid4())
    store.bind_organization(org, secrets.token_urlsafe(32))
    store.stage_agent(
        HostAgentConfiguration(
            host_id=store.instance_id,
            organization_id=org,
            agent_id=agent,
            version=1,
            name="Manager",
        )
    )
    token = store.agent(org, agent)["agent_token"]
    requested = []

    def redirected(request):
        requested.append(request)
        return Response(307, headers={"Location": "https://untrusted.example/collect"})

    app.state.organization_management.transport = MockTransport(redirected)

    async def check():
        async with (
            app.state.mcp_server.session_manager.run(),
            AsyncClient(transport=ASGITransport(app), base_url="http://localhost/mcp") as client,
        ):
            result = await _request(
                client, token, "tools/call", {"name": "organization_list_agents", "arguments": {}}
            )
            assert result.json()["result"]["isError"]
            assert len(requested) == 1
            assert token not in result.text
            app.state.organization_management.transport = MockTransport(
                lambda request: Response(403, json={"detail": "hidden permission state"})
            )
            denied = await _request(
                client, token, "tools/call", {"name": "organization_list_agents", "arguments": {}}
            )
            assert denied.json()["result"]["isError"]
            assert "hidden permission state" not in denied.text
            assert "Organization operation is not permitted" in denied.text

    asyncio.run(check())
