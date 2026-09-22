import asyncio

from httpx import ASGITransport, AsyncClient

from fesnyng_backend import agent_management
from fesnyng_backend.agent_host import create_app as create_host_app
from fesnyng_backend.control_plane import create_app
from fesnyng_backend.host_client import HostClient, HostResponse, HostUnavailable
from fesnyng_backend.host_models import HostAgentConfiguration
from fesnyng_backend.settings import ControlPlaneSessionSettings, ServiceSettings

ORIGIN = "https://control.example"


async def _sign_in(client: AsyncClient) -> None:
    response = await client.post(
        "/auth/login", json={"login": "owner", "password": "correct horse battery staple"}
    )
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]


async def _management_api(organization, monkeypatch) -> None:
    settings, control, _owner, org, agents, host_id = organization
    app = create_app(settings, ControlPlaneSessionSettings(allowed_origin=ORIGIN))
    base = f"/organizations/{org.id}"

    async with AsyncClient(
        transport=ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
    ) as human:
        await _sign_in(human)
        caller = await human.post(f"{base}/agents", json={"name": "CEO", "host_id": host_id})
        assert caller.status_code == 201
        caller_id = caller.json()["id"]
        assert (await human.get(f"{base}/agents/{caller_id}/management")).json() == {
            "enabled": False
        }
        assert "management" not in caller.json()
        assert (
            await human.patch(
                f"{base}/agents/{caller_id}",
                json={"expected_version": 1, "management": {"enabled": True}},
            )
        ).status_code == 422
        assert (
            await human.patch(
                f"{base}/agents/{caller_id}",
                json={
                    "expected_version": 1,
                    "configuration": {"management": {"enabled": True}},
                },
            )
        ).status_code == 422
        assert (
            await human.put(f"{base}/agents/{caller_id}/management", json={"enabled": True})
        ).status_code == 200

        class AuthenticatedHost:
            async def raw_request(self, organization_id, host, path, *, method="GET", **kwargs):
                assert organization_id == str(org.id)
                assert host == caller.json()["host_id"]
                assert path == f"/agents/{caller_id}/agent-authenticate"
                assert method == "POST"
                assert kwargs["headers"] == {"X-Fesnyng-Agent-Token": "agent-secret"}
                return HostResponse(204, b"", None)

            async def apply(self, organization_id, agent_id):
                agent = agents.get_agent(organization_id, agent_id)
                agents.acknowledge_host(
                    organization_id, agent_id, agent["host_id"], agent["desired_version"]
                )
                return agents.get_agent(organization_id, agent_id)

            async def request(self, organization_id, host, path, *, body, **kwargs):
                assert path == "/peers"
                return {
                    "organization_id": organization_id,
                    "host_id": host,
                    "version": body["version"],
                }

        monkeypatch.setattr(
            "fesnyng_backend.agent_management.host_client", lambda request: AuthenticatedHost()
        )
        headers = {"Authorization": "Bearer agent-secret"}
        created = await human.post(
            f"/agent-api{base}/agents/{caller_id}/agents",
            headers=headers,
            json={
                "name": "Engineer",
                "host_id": host_id,
                "configuration": {"instructions": "Build the service."},
            },
        )
        assert created.status_code == 201
        engineer = created.json()
        assert engineer["configuration"]["instructions"] == "Build the service."
        assert engineer["configuration_status"] == "applied"
        assert (
            await human.get(f"/agent-api{base}/agents/{caller_id}/resources", headers=headers)
        ).json() == {
            "hosts": [{"id": host_id, "name": "Local host"}],
            "profiles": [],
        }
        department = await human.post(
            f"/agent-api{base}/agents/{caller_id}/departments",
            headers=headers,
            json={"name": "Engineering"},
        )
        assert department.status_code == 201
        changed = await human.patch(
            f"/agent-api{base}/agents/{caller_id}/agents/{engineer['id']}",
            headers=headers,
            json={
                "expected_version": 1,
                "department_id": department.json()["id"],
                "configuration": {"instructions": "Ship it."},
            },
        )
        assert changed.status_code == 200
        assert changed.json()["configuration"]["instructions"] == "Ship it."
        assert (
            await human.patch(
                f"/agent-api{base}/agents/{caller_id}/agents/{engineer['id']}",
                headers=headers,
                json={"expected_version": 1, "name": "Stale"},
            )
        ).status_code == 409
        assert (
            await human.patch(
                f"/agent-api{base}/agents/{caller_id}/agents/{engineer['id']}",
                headers=headers,
                json={"expected_version": 2, "configuration": {"runtime_type": "codex"}},
            )
        ).status_code == 422

    with control.connect() as connection:
        provenance = connection.execute(
            "SELECT created_by,created_by_agent FROM agent_configurations "
            "WHERE agent_id=? ORDER BY version",
            (engineer["id"],),
        ).fetchall()
    assert [(row["created_by"], row["created_by_agent"]) for row in provenance] == [
        (None, caller_id),
        (None, caller_id),
    ]


def test_enabled_agent_can_manage_organization_with_agent_provenance(organization, monkeypatch):
    asyncio.run(_management_api(organization, monkeypatch))


async def _authorization_and_pending_apply(organization, monkeypatch) -> None:
    settings, _control, _owner, org, _agents, host_id = organization
    app = create_app(settings, ControlPlaneSessionSettings(allowed_origin=ORIGIN))
    base = f"/organizations/{org.id}"

    class AuthenticatedButUnavailableHost:
        async def raw_request(self, *args, **kwargs):
            return HostResponse(204, b"", None)

        async def apply(self, *args, **kwargs):
            raise HostUnavailable("Agent host is unreachable")

        async def request(self, organization_id, host, path, *, body, **kwargs):
            return {
                "organization_id": organization_id,
                "host_id": host,
                "version": body["version"],
            }

    monkeypatch.setattr(
        "fesnyng_backend.agent_management.host_client",
        lambda request: AuthenticatedButUnavailableHost(),
    )
    async with AsyncClient(
        transport=ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
    ) as human:
        await _sign_in(human)
        caller = await human.post(f"{base}/agents", json={"name": "CEO", "host_id": host_id})
        caller_id = caller.json()["id"]
        headers = {"Authorization": "Bearer agent-secret"}
        async with AsyncClient(
            transport=ASGITransport(app),
            base_url=ORIGIN,
            headers={**headers, "Origin": ORIGIN},
        ) as agent_only:
            assert (
                await agent_only.get(f"{base}/agents/{caller_id}/management")
            ).status_code == 401
            assert (
                await agent_only.put(
                    f"{base}/agents/{caller_id}/management", json={"enabled": True}
                )
            ).status_code == 401
        member = await human.put(
            f"{base}/members",
            json={
                "login": "member",
                "display_name": "Member",
                "password": "a separate member password",
                "role": "member",
            },
        )
        assert member.status_code == 200
        async with AsyncClient(
            transport=ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
        ) as member_client:
            response = await member_client.post(
                "/auth/login", json={"login": "member", "password": "a separate member password"}
            )
            member_client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
            assert (
                await member_client.get(f"{base}/agents/{caller_id}/management")
            ).status_code == 403
            assert (
                await member_client.put(
                    f"{base}/agents/{caller_id}/management", json={"enabled": True}
                )
            ).status_code == 403
        csrf = human.headers.pop("X-CSRF-Token")
        assert (
            await human.put(f"{base}/agents/{caller_id}/management", json={"enabled": True})
        ).status_code == 403
        human.headers["X-CSRF-Token"] = csrf
        ungranted = await human.get(f"/agent-api{base}/agents/{caller_id}/agents", headers=headers)
        assert ungranted.status_code == 403
        assert ungranted.json()["detail"] == "Organization operation is not permitted"
        assert (
            await human.put(f"{base}/agents/{caller_id}/management", json={"enabled": True})
        ).status_code == 200
        pending = await human.post(
            f"/agent-api{base}/agents/{caller_id}/agents",
            headers=headers,
            json={"name": "Pending", "host_id": host_id},
        )
        assert pending.status_code == 202
        assert pending.json()["agent"]["configuration_status"] == "pending"
        assert pending.json()["apply_error"] == "Agent host is unreachable"
        assert (
            await human.put(f"{base}/agents/{caller_id}/management", json={"enabled": False})
        ).status_code == 200
        assert (
            await human.get(f"/agent-api{base}/agents/{caller_id}/agents", headers=headers)
        ).status_code == 403


def test_agent_management_rechecks_grants_and_preserves_pending_apply(organization, monkeypatch):
    asyncio.run(_authorization_and_pending_apply(organization, monkeypatch))


def test_existing_human_configuration_provenance_migrates_without_reassignment(organization):
    settings, control, owner, org, agents, host_id = organization
    existing = agents.create_agent(str(org.id), owner.id, {"name": "Existing", "host_id": host_id})
    with control.connect() as connection:
        connection.execute(
            "ALTER TABLE agent_configurations RENAME TO agent_configurations_current"
        )
        connection.execute(
            """CREATE TABLE agent_configurations (
            agent_id TEXT NOT NULL REFERENCES agents(id),
            version INTEGER NOT NULL,
            configuration TEXT NOT NULL,
            created_by TEXT NOT NULL REFERENCES users(id),
            PRIMARY KEY (agent_id, version)
            )"""
        )
        connection.execute(
            "INSERT INTO agent_configurations SELECT agent_id,version,configuration,created_by "
            "FROM agent_configurations_current"
        )
        connection.execute("DROP TABLE agent_configurations_current")
        connection.execute("UPDATE control_plane_schema SET schema_version=4 WHERE singleton=1")
    create_app(settings, ControlPlaneSessionSettings(allowed_origin=ORIGIN))
    with control.connect() as connection:
        columns = {
            row["name"]: row
            for row in connection.execute("PRAGMA table_info(agent_configurations)")
        }
        row = connection.execute(
            "SELECT created_by,created_by_agent FROM agent_configurations WHERE agent_id=?",
            (existing["id"],),
        ).fetchone()
    assert columns["created_by"]["notnull"] == 0
    assert "created_by_agent" in columns
    assert tuple(row) == (owner.id, None)


async def _control_to_host_agent_authentication(organization, monkeypatch, tmp_path) -> None:
    settings, control, owner, org, agents, _host_id = organization
    host_app = create_host_app(
        ServiceSettings(
            service="agent-host",
            database_path=tmp_path / "host.sqlite3",
            state_directory=tmp_path / "host-state",
        )
    )
    host = host_app.state.host_store
    binding = "h" * 32
    host.bind_organization(org.id, binding)
    agents.register_host(str(host.instance_id), "Integration host", "http://host.test", org.id)
    agents.set_host_credential(org.id, str(host.instance_id), binding)
    with control.connect() as connection:
        connection.execute(
            "DELETE FROM organization_hosts WHERE organization_id=? AND host_id=?",
            (org.id, _host_id),
        )
    caller = agents.create_agent(
        org.id, owner.id, {"name": "CEO", "host_id": str(host.instance_id)}
    )
    host.stage_agent(
        HostAgentConfiguration(
            host_id=host.instance_id,
            organization_id=org.id,
            agent_id=caller["id"],
            version=1,
            name="CEO",
        )
    )
    caller_token = host.agent(org.id, caller["id"])["agent_token"]
    second = agents.create_agent(
        org.id, owner.id, {"name": "Other", "host_id": str(host.instance_id)}
    )
    host.stage_agent(
        HostAgentConfiguration(
            host_id=host.instance_id,
            organization_id=org.id,
            agent_id=second["id"],
            version=1,
            name="Other",
        )
    )
    other_org = control.create_organization(owner.id, "Other organization")
    app = create_app(settings, ControlPlaneSessionSettings(allowed_origin=ORIGIN))
    monkeypatch.setattr(
        agent_management,
        "host_client",
        lambda request: HostClient(agents, transport=ASGITransport(host_app)),
    )
    base = f"/organizations/{org.id}/agents/{caller['id']}"
    headers = {"Authorization": f"Bearer {caller_token}"}

    async with AsyncClient(
        transport=ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
    ) as client:
        await _sign_in(client)
        assert (
            await client.put(
                f"/organizations/{org.id}/agents/{caller['id']}/management", json={"enabled": True}
            )
        ).status_code == 200
        valid = await client.get(f"/agent-api{base}/resources", headers=headers)
        assert valid.status_code == 200
        assert binding not in valid.text and caller_token not in valid.text
        assert valid.json()["hosts"] == [{"id": str(host.instance_id), "name": "Integration host"}]
        assert valid.json()["profiles"] == []
        assert (
            await client.get(
                f"/agent-api/organizations/{org.id}/agents/{second['id']}/agents", headers=headers
            )
        ).status_code == 401
        assert (
            await client.get(
                f"/agent-api/organizations/{other_org.id}/agents/{caller['id']}/agents",
                headers=headers,
            )
        ).status_code == 401
        ordinary_edit = await client.patch(
            f"/organizations/{org.id}/agents/{caller['id']}",
            json={"expected_version": 1, "title": "Chief executive"},
        )
        assert ordinary_edit.status_code == 200
        assert (
            await client.get(f"/organizations/{org.id}/agents/{caller['id']}/management")
        ).json() == {"enabled": True}
        assert (await client.get(f"/agent-api{base}/agents", headers=headers)).status_code == 200
        created = await client.post(
            f"/agent-api{base}/agents",
            headers=headers,
            json={
                "name": "Engineer",
                "host_id": str(host.instance_id),
                "reports_to_agent_id": caller["id"],
            },
        )
        assert created.status_code == 202
        created_agent = created.json()["agent"]
        roster = host_app.state.peer_configuration.get(org.id)
        assert {
            (agent["agent_id"], agent["reports_to_agent_id"]) for agent in roster["agents"]
        } >= {(created_agent["id"], caller["id"])}
        assert (
            await client.put(
                f"/organizations/{org.id}/agents/{caller['id']}/management", json={"enabled": False}
            )
        ).status_code == 200
        revoked = await client.get(f"/agent-api{base}/agents", headers=headers)
        assert revoked.status_code == 403
        assert revoked.json()["detail"] == "Organization operation is not permitted"


def test_agent_api_authenticates_against_assigned_host_and_reconciles_peers(
    organization, monkeypatch, tmp_path
):
    asyncio.run(_control_to_host_agent_authentication(organization, monkeypatch, tmp_path))


async def _post_save_timeout_preserves_department(organization, monkeypatch) -> None:
    settings, _control, _owner, org, _agents, host_id = organization
    app = create_app(settings, ControlPlaneSessionSettings(allowed_origin=ORIGIN))
    base = f"/organizations/{org.id}"

    class SlowPeerHost:
        async def raw_request(self, *args, **kwargs):
            return HostResponse(204, b"", None)

        async def request(self, *args, **kwargs):
            await asyncio.sleep(0.05)
            return {}

    monkeypatch.setattr(agent_management, "host_client", lambda request: SlowPeerHost())
    monkeypatch.setattr(agent_management, "POST_SAVE_RECONCILIATION_TIMEOUT_SECONDS", 0.001)
    async with AsyncClient(
        transport=ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
    ) as client:
        await _sign_in(client)
        caller = await client.post(f"{base}/agents", json={"name": "CEO", "host_id": host_id})
        caller_id = caller.json()["id"]
        assert (
            await client.put(f"{base}/agents/{caller_id}/management", json={"enabled": True})
        ).status_code == 200
        result = await client.post(
            f"/agent-api{base}/agents/{caller_id}/departments",
            headers={"Authorization": "Bearer agent-secret"},
            json={"name": "Engineering"},
        )
        assert result.status_code == 202
        assert result.json()["department"]["name"] == "Engineering"
        assert result.json()["peer_apply_error"].endswith("timeout")
        assert (
            await client.get(
                f"/agent-api{base}/agents/{caller_id}/departments",
                headers={"Authorization": "Bearer agent-secret"},
            )
        ).json()[0]["name"] == "Engineering"


def test_post_save_timeout_keeps_desired_department_pending(organization, monkeypatch):
    asyncio.run(_post_save_timeout_preserves_department(organization, monkeypatch))


async def _agent_authentication_timeout(organization, monkeypatch) -> None:
    settings, _control, _owner, org, _agents, host_id = organization
    app = create_app(settings, ControlPlaneSessionSettings(allowed_origin=ORIGIN))

    class SlowAuthenticationHost:
        async def raw_request(self, *args, **kwargs):
            await asyncio.sleep(0.05)
            return HostResponse(204, b"", None)

    monkeypatch.setattr(agent_management, "host_client", lambda request: SlowAuthenticationHost())
    monkeypatch.setattr(agent_management, "AGENT_AUTHENTICATION_TIMEOUT_SECONDS", 0.001)
    async with AsyncClient(
        transport=ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
    ) as client:
        await _sign_in(client)
        caller = await client.post(
            f"/organizations/{org.id}/agents", json={"name": "CEO", "host_id": host_id}
        )
        response = await client.get(
            f"/agent-api/organizations/{org.id}/agents/{caller.json()['id']}/agents",
            headers={"Authorization": "Bearer agent-secret"},
        )
    assert response.status_code == 503
    assert response.json()["detail"] == "Agent authentication service is unavailable"


def test_agent_authentication_timeout_is_bounded(organization, monkeypatch):
    asyncio.run(_agent_authentication_timeout(organization, monkeypatch))
