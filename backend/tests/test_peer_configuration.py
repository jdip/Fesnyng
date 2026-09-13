import asyncio
import json
import secrets
from uuid import uuid4

import httpx
import pytest

from fesnyng_backend import control_peer_routes
from fesnyng_backend.agent_host import create_app as create_host_app
from fesnyng_backend.control_plane import create_app as create_control_app
from fesnyng_backend.host_client import HostClient
from fesnyng_backend.host_store import HostStore
from fesnyng_backend.peer_configuration import ControlPeerConfigurationStore
from fesnyng_backend.peer_configuration_routes import router as peer_router
from fesnyng_backend.settings import ControlPlaneSessionSettings, ServiceSettings

ORIGIN = "https://control.example"


def test_host_peer_configuration_is_durable_scoped_and_redacts_tokens(tmp_path):
    from fesnyng_backend.peer_configuration import (
        PeerAgent,
        PeerConfiguration,
        PeerConfigurationStore,
        PeerHost,
    )

    host = HostStore(
        ServiceSettings(
            service="agent-host",
            state_directory=tmp_path / "state",
            database_path=tmp_path / "state/host.sqlite3",
        )
    )
    host.initialize()
    organization_id = str(uuid4())
    host.bind_organization(organization_id, "organization binding token with enough characters")
    own_agent, remote_agent, remote_host = uuid4(), uuid4(), uuid4()
    outbound, inbound = (
        "outbound-token-with-at-least-thirty-two-characters",
        "inbound-token-with-at-least-thirty-two-characters",
    )
    configuration = PeerConfiguration(
        organization_id=organization_id,
        host_id=host.instance_id,
        version=1,
        agents=[
            PeerAgent(
                agent_id=own_agent, name="Host agent", title="Engineer", host_id=host.instance_id
            ),
            PeerAgent(
                agent_id=remote_agent, name="Remote agent", title="Reviewer", host_id=remote_host
            ),
        ],
        peers=[
            PeerHost(
                host_id=remote_host,
                origin="https://peer.example",
                outbound_token=outbound,
                inbound_token=inbound,
            )
        ],
    )
    store = PeerConfigurationStore(host)
    store.initialize()

    assert store.apply(configuration) == {
        "organization_id": organization_id,
        "host_id": str(host.instance_id),
        "version": 1,
        "agent_count": 2,
        "peer_host_ids": [str(remote_host)],
    }
    assert store.status(organization_id)["version"] == 1
    assert outbound not in str(store.status(organization_id))
    assert store.authenticate(organization_id, inbound) == str(remote_host)
    assert store.authenticate(organization_id, outbound) is None
    assert store.connection(organization_id, str(remote_host)) == ("https://peer.example", outbound)
    assert store.agent(organization_id, str(remote_agent)) == {
        "agent_id": str(remote_agent),
        "name": "Remote agent",
        "title": "Reviewer",
        "host_id": str(remote_host),
        "reports_to_agent_id": None,
    }
    restored = PeerConfigurationStore(HostStore(host.settings))
    assert restored.get(organization_id)["peers"][0]["inbound_token"] == inbound
    with pytest.raises(ValueError, match="conflict"):
        store.apply(configuration.model_copy(update={"agents": configuration.agents[:1]}))
    with pytest.raises(ValueError, match="stale"):
        store.apply(configuration.model_copy(update={"version": 0}))


def test_peer_host_routes_require_management_for_config_and_peer_token_for_inbound(tmp_path):
    from fastapi import Request

    from fesnyng_backend.peer_configuration import (
        PeerAgent,
        PeerConfiguration,
        PeerConfigurationStore,
        PeerHost,
        require_peer,
    )

    settings = ServiceSettings(
        service="agent-host",
        state_directory=tmp_path / "state",
        database_path=tmp_path / "state/host.sqlite3",
    )
    app = create_host_app(settings)
    app.state.peer_configuration = PeerConfigurationStore(app.state.host_store)
    app.state.peer_configuration.initialize()
    organization_id, remote_host = uuid4(), uuid4()
    binding, peer_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    app.state.host_store.bind_organization(str(organization_id), binding)
    app.include_router(peer_router)

    @app.post("/inbound")
    def inbound(request: Request):
        return {"source": require_peer(request, str(organization_id))}

    configuration = PeerConfiguration(
        organization_id=organization_id,
        host_id=app.state.host_store.instance_id,
        version=1,
        agents=[PeerAgent(agent_id=uuid4(), name="Peer", host_id=remote_host)],
        peers=[
            PeerHost(
                host_id=remote_host,
                origin="https://peer.example",
                outbound_token=secrets.token_urlsafe(32),
                inbound_token=peer_token,
            )
        ],
    )

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://host"
        ) as client:
            path = f"/organizations/{organization_id}/peers"
            assert (
                await client.put(path, json=configuration.model_dump(mode="json"))
            ).status_code == 401
            applied = await client.put(
                path,
                headers={"Authorization": f"Bearer {binding}"},
                json=configuration.model_dump(mode="json"),
            )
            assert applied.status_code == 200
            assert peer_token not in applied.text
            assert (
                await client.post("/inbound", headers={"Authorization": f"Bearer {binding}"})
            ).status_code == 401
            authenticated = await client.post(
                "/inbound", headers={"Authorization": f"Bearer {peer_token}"}
            )
            assert authenticated.json() == {"source": str(remote_host)}

    asyncio.run(exercise())


def test_control_apply_versions_roster_and_reports_partial_host_failures(organization, monkeypatch):
    settings, _, owner, org, agents, first_host = organization
    second_host = str(uuid4())
    agents.register_host(second_host, "Second host", "https://second.example", org.id)
    for host_id in (first_host, second_host):
        agents.set_host_credential(org.id, host_id, secrets.token_urlsafe(32))
    agents.create_agent(org.id, owner.id, {"name": "First", "host_id": first_host})
    agents.create_agent(org.id, owner.id, {"name": "Second", "host_id": second_host})
    calls = 0

    def host(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = json.loads(request.content)
        if calls == 2:
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "organization_id": body["organization_id"],
                "host_id": body["host_id"],
                "version": body["version"],
            },
        )

    client = HostClient(agents, transport=httpx.MockTransport(host))
    monkeypatch.setattr(control_peer_routes, "host_client", lambda _: client)
    app = create_control_app(settings, ControlPlaneSessionSettings(allowed_origin=ORIGIN))

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
        ) as http:
            login = await http.post(
                "/auth/login", json={"login": "owner", "password": "correct horse battery staple"}
            )
            assert login.status_code == 200
            http.headers["X-CSRF-Token"] = login.json()["csrf_token"]
            applied = await http.post(f"/organizations/{org.id}/peers/apply")
            assert applied.status_code == 200
            result = applied.json()
            assert result["desired_version"] == 1
            assert [host["status"] for host in result["hosts"]] == ["applied", "pending"]
            repeated = await http.post(f"/organizations/{org.id}/peers/apply")
            assert repeated.json()["desired_version"] == 1
            assert (await http.get(f"/organizations/{org.id}/peers")).status_code == 200

    asyncio.run(exercise())


def test_control_peer_status_keeps_newer_confirmation_when_late_result_arrives(organization):
    _, control, _, org, agents, first_host = organization
    configurations = ControlPeerConfigurationStore(control)
    assert configurations.desired(org.id)["version"] == 1
    agents.register_host(str(uuid4()), "Second host", "https://second.example", org.id)
    assert configurations.desired(org.id)["version"] == 2

    configurations.record_application(org.id, first_host, 2, applied=True)
    configurations.record_application(org.id, first_host, 1, applied=False)

    status = configurations.status(org.id)
    first = next(host for host in status["hosts"] if host["host_id"] == first_host)
    assert first == {
        "host_id": first_host,
        "desired_version": 2,
        "applied_version": 2,
        "status": "applied",
        "error": None,
    }


def test_control_apply_freezes_each_host_envelope_before_awaiting_hosts(organization, monkeypatch):
    settings, _, owner, org, agents, first_host = organization
    second_host = str(uuid4())
    agents.register_host(second_host, "Second host", "https://second.example", org.id)
    for host_id in (first_host, second_host):
        agents.set_host_credential(org.id, host_id, secrets.token_urlsafe(32))
    versions: list[int] = []

    def host(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        versions.append(body["version"])
        if len(versions) == 1:
            agents.create_agent(org.id, owner.id, {"name": "Changed roster", "host_id": first_host})
        return httpx.Response(
            200,
            json={
                "organization_id": body["organization_id"],
                "host_id": body["host_id"],
                "version": body["version"],
            },
        )

    monkeypatch.setattr(
        control_peer_routes,
        "host_client",
        lambda _: HostClient(agents, transport=httpx.MockTransport(host)),
    )
    app = create_control_app(settings, ControlPlaneSessionSettings(allowed_origin=ORIGIN))

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
        ) as http:
            login = await http.post(
                "/auth/login", json={"login": "owner", "password": "correct horse battery staple"}
            )
            http.headers["X-CSRF-Token"] = login.json()["csrf_token"]
            response = await http.post(f"/organizations/{org.id}/peers/apply")
            assert response.status_code == 200
            assert response.json()["desired_version"] == 2
            assert {host["status"] for host in response.json()["hosts"]} == {"pending"}

    asyncio.run(exercise())
    assert versions == [1, 1]
