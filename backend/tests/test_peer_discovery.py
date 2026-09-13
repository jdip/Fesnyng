import asyncio
import secrets
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from fesnyng_backend.host_models import HostAgentConfiguration
from fesnyng_backend.host_runtime import RuntimeUnavailable
from fesnyng_backend.host_store import HostStore
from fesnyng_backend.peer_configuration import PeerConfiguration, PeerConfigurationStore
from fesnyng_backend.peer_discovery import DiscoveryQuery, PeerDiscovery
from fesnyng_backend.peer_discovery_routes import router
from fesnyng_backend.settings import ServiceSettings


def discovery_system(tmp_path):
    host = HostStore(
        ServiceSettings(
            service="agent-host",
            database_path=tmp_path / "host.db",
            state_directory=tmp_path / "state",
        )
    )
    host.initialize()
    org = str(uuid4())
    host.bind_organization(org, secrets.token_urlsafe(32))
    roster = []
    for name in ("CEO", "Senior", "Junior", "Other"):
        aid = str(uuid4())
        manager = roster[-1]["agent_id"] if name in {"Senior", "Junior"} else None
        roster.append(
            {
                "agent_id": aid,
                "name": name,
                "title": "",
                "host_id": str(host.instance_id),
                "reports_to_agent_id": manager,
            }
        )
        envelope = HostAgentConfiguration(
            host_id=host.instance_id, organization_id=org, agent_id=aid, version=1, name=name
        )
        host.stage_agent(envelope)
        host.mark_applied(envelope)
        host.save_session(
            org, aid, f"ses_{name}", f"/workspace/default/threads/{name}", f"{name} work"
        )
    config = PeerConfigurationStore(host)
    config.initialize()
    config.apply(
        PeerConfiguration.model_validate(
            {
                "host_id": str(host.instance_id),
                "organization_id": org,
                "version": 1,
                "agents": roster,
                "peers": [],
            }
        )
    )
    return host, config, org, roster


def test_discovery_prefers_nearby_colleagues_without_excluding_distant_agents(tmp_path):
    host, config, org, roster = discovery_system(tmp_path)

    class Native:
        async def request(self, organization_id, agent_id, path, *, directory=None):
            assert organization_id == org
            if path == "/session/status":
                return {"ses_Junior": {"type": "busy"}}
            return [
                {
                    "info": {"id": "msg_one", "sessionID": path.split("/")[2]},
                    "parts": [{"type": "text", "text": "Investigating compiler regressions"}],
                }
            ]

    async def check():
        discovery = PeerDiscovery(host, config, Native())
        result = await discovery.discover(org, roster[1]["agent_id"], DiscoveryQuery())
        assert [row["name"] for row in result["agents"]] == ["Senior", "CEO", "Junior", "Other"]
        assert result["agents"][-1]["chart_distance"] is None
        assert len(result["threads"]) == 4
        filtered = await discovery.discover(
            org, roster[1]["agent_id"], DiscoveryQuery(topic="compiler", active=True)
        )
        assert [row["session_id"] for row in filtered["threads"]] == ["ses_Junior"]
        assert filtered["unavailable"] == []

    asyncio.run(check())


@pytest.mark.parametrize("remote", [False, True])
def test_read_rejects_history_from_a_different_native_thread(tmp_path, remote):
    host, config, org, roster = discovery_system(tmp_path)
    target = roster[0]["agent_id"]
    history = [{"info": {"id": "msg_wrong", "sessionID": "ses_other"}, "parts": []}]
    if remote:
        peer_id, target = str(uuid4()), str(uuid4())
        configuration = config.get(org)
        configuration["version"] = 2
        configuration["peers"] = [
            {
                "host_id": peer_id,
                "origin": "http://peer.test",
                "outbound_token": secrets.token_urlsafe(32),
                "inbound_token": secrets.token_urlsafe(32),
            }
        ]
        configuration["agents"].append(
            {"agent_id": target, "name": "Remote colleague", "host_id": peer_id}
        )
        config.apply(PeerConfiguration.model_validate(configuration))

    class Native:
        async def request(self, organization_id, agent_id, path, *, directory=None):
            return history

    async def check():
        discovery = PeerDiscovery(
            host,
            config,
            Native(),
            httpx.MockTransport(lambda request: httpx.Response(200, json=history)),
        )
        with pytest.raises(RuntimeUnavailable, match="another thread"):
            await discovery.read(org, roster[0]["agent_id"], target, "ses_CEO")

    asyncio.run(check())


@pytest.mark.parametrize("malformed", ["activity", "activity_list", "parts", "text"])
def test_malformed_native_agent_does_not_hide_healthy_threads(tmp_path, malformed):
    host, config, org, roster = discovery_system(tmp_path)

    class Native:
        async def request(self, organization_id, agent_id, path, *, directory=None):
            if (
                path == "/session/status"
                and agent_id == roster[0]["agent_id"]
                and malformed == "activity_list"
            ):
                return {"ses_CEO": {"type": []}}
            if path == "/session/status":
                return (
                    {"ses_CEO": None}
                    if agent_id == roster[0]["agent_id"] and malformed == "activity"
                    else {}
                )
            if agent_id == roster[0]["agent_id"]:
                return [
                    {"parts": None if malformed == "parts" else [{"type": "text", "text": None}]}
                ]
            return [
                {
                    "info": {"id": "msg_one", "sessionID": path.split("/")[2]},
                    "parts": [{"type": "text", "text": "compiler"}],
                }
            ]

    async def check():
        result = await PeerDiscovery(host, config, Native()).discover(
            org, roster[1]["agent_id"], DiscoveryQuery(topic="compiler")
        )
        assert len(result["threads"]) == 3
        assert result["unavailable"][0]["agent_id"] == roster[0]["agent_id"]

    asyncio.run(check())


def test_peer_read_authenticates_source_host_and_rejects_forged_agent_and_other_org(tmp_path):
    host, config, org, roster = discovery_system(tmp_path)
    peer_id, source_id = str(uuid4()), str(uuid4())
    token = secrets.token_urlsafe(32)
    configuration = config.get(org)
    configuration["version"] = 2
    configuration["peers"] = [
        {
            "host_id": peer_id,
            "origin": "http://peer.test",
            "outbound_token": secrets.token_urlsafe(32),
            "inbound_token": token,
        }
    ]
    configuration["agents"].append(
        {"agent_id": source_id, "name": "Remote colleague", "host_id": peer_id}
    )
    config.apply(PeerConfiguration.model_validate(configuration))

    class Native:
        async def request(self, organization_id, agent_id, path, *, directory=None):
            assert organization_id == org and agent_id == roster[0]["agent_id"]
            assert path == "/session/ses_CEO/message"
            return [
                {
                    "info": {"id": "msg_one", "sessionID": "ses_CEO"},
                    "parts": [{"type": "text", "text": "Shared result"}],
                }
            ]

    app = FastAPI()
    app.state.peer_configuration = config
    app.state.peer_discovery = PeerDiscovery(host, config, Native())
    app.include_router(router)

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://host.test",
            headers={"Authorization": f"Bearer {token}"},
        ) as client:
            body = {
                "source_agent": source_id,
                "agent_id": roster[0]["agent_id"],
                "session_id": "ses_CEO",
            }
            response = await client.post(f"/organizations/{org}/peer/read", json=body)
            assert response.status_code == 200
            assert response.json()[0]["parts"][0]["text"] == "Shared result"
            response = await client.post(
                f"/organizations/{org}/peer/read",
                json={**body, "source_agent": roster[1]["agent_id"]},
            )
            assert response.status_code == 403
            response = await client.post(f"/organizations/{uuid4()}/peer/read", json=body)
            assert response.status_code in {401, 403}
            response = await client.post(
                f"/organizations/{org}/peer/read", json={**body, "session_id": "ses_Senior"}
            )
            assert response.status_code == 404

    asyncio.run(check())


@pytest.mark.parametrize(
    "reply", [{}, {"threads": [{"organization_id": "foreign"}], "unavailable": []}]
)
def test_malformed_peer_discovery_preserves_local_results_and_reports_unavailable(tmp_path, reply):
    host, config, org, roster = discovery_system(tmp_path)
    peer_id = str(uuid4())
    configuration = config.get(org)
    configuration.update(
        version=2,
        peers=[
            {
                "host_id": peer_id,
                "origin": "http://peer.test",
                "outbound_token": secrets.token_urlsafe(32),
                "inbound_token": secrets.token_urlsafe(32),
            }
        ],
    )
    config.apply(PeerConfiguration.model_validate(configuration))

    class Native:
        async def request(self, organization_id, agent_id, path, *, directory=None):
            return {}

    async def check():
        discovery = PeerDiscovery(
            host,
            config,
            Native(),
            httpx.MockTransport(lambda request: httpx.Response(200, json=reply)),
        )
        result = await discovery.discover(org, roster[0]["agent_id"], DiscoveryQuery())
        assert len(result["threads"]) == 4
        assert result["unavailable"] == [
            {"host_id": peer_id, "reason": "Peer discovery unavailable"}
        ]

    asyncio.run(check())
