import json
import secrets
from uuid import uuid4

import pytest

from fesnyng_backend.host_models import HostAgentConfiguration
from fesnyng_backend.host_store import HostStore
from fesnyng_backend.settings import ServiceSettings


def test_host_bindings_and_agent_identity_survive_restart_and_deny_foreign_org(tmp_path):
    settings = ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "host.sqlite3",
        state_directory=tmp_path / "state",
    )
    host = HostStore(settings)
    host.initialize()
    org, other = str(uuid4()), str(uuid4())
    token = secrets.token_urlsafe(32)
    host.bind_organization(org, token)
    assert host.authenticate(token) == org
    assert host.authenticate("unknown") is None
    envelope = HostAgentConfiguration(
        host_id=host.instance_id, organization_id=org, agent_id=uuid4(), version=1, name="Engineer"
    )
    host.stage_agent(envelope)
    agent = host.agent(org, str(envelope.agent_id))
    assert agent["applied_envelope"] is None
    with pytest.raises(LookupError):
        host.agent(other, str(envelope.agent_id))
    restarted = HostStore(settings)
    restarted.initialize()
    assert restarted.instance_id == host.instance_id
    assert restarted.agent(org, str(envelope.agent_id)) == agent
    host.mark_applied(envelope)
    assert host.agent_status(org, str(envelope.agent_id))["applied_version"] == 1
    assert host.stage_agent(envelope) is False
    with pytest.raises(ValueError, match="conflict"):
        host.stage_agent(envelope.model_copy(update={"name": "Conflicting name"}))


def test_legacy_sessions_receive_immutable_opencode_provenance_without_rewriting_agent_state(
    tmp_path,
):
    settings = ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "host.sqlite3",
        state_directory=tmp_path / "state",
    )
    host = HostStore(settings)
    host.initialize()
    organization_id, agent_id = str(uuid4()), str(uuid4())
    host.bind_organization(organization_id, secrets.token_urlsafe(32))
    envelope = HostAgentConfiguration(
        host_id=host.instance_id,
        organization_id=organization_id,
        agent_id=agent_id,
        version=1,
        name="Engineer",
    )
    host.stage_agent(envelope)
    host.mark_applied(envelope)
    legacy_envelope = envelope.model_dump(mode="json")
    del legacy_envelope["configuration"]["runtime_type"]
    legacy_envelope_json = json.dumps(legacy_envelope)

    # Simulate a retained host database created before thread harness bindings.
    with host.connect() as connection:
        connection.execute(
            "UPDATE host_agents SET desired_envelope=?,applied_envelope=? WHERE agent_id=?",
            (legacy_envelope_json, legacy_envelope_json, agent_id),
        )
        connection.execute("ALTER TABLE host_sessions RENAME TO migrated_host_sessions")
        connection.execute(
            """CREATE TABLE host_sessions (
                session_id TEXT PRIMARY KEY, organization_id TEXT NOT NULL,
                agent_id TEXT NOT NULL, directory TEXT NOT NULL, title TEXT NOT NULL,
                deleted_at INTEGER, archived_at INTEGER,
                created_at INTEGER NOT NULL DEFAULT (unixepoch()),
                FOREIGN KEY(organization_id,agent_id) REFERENCES host_agents(organization_id,agent_id)
            )"""
        )
        connection.execute(
            "INSERT INTO host_sessions(session_id,organization_id,agent_id,directory,title) "
            "VALUES(?,?,?,?,?)",
            ("ses_legacy", organization_id, agent_id, "/workspace/legacy", "Legacy"),
        )
        connection.execute("DROP TABLE migrated_host_sessions")

    HostStore(settings).initialize()
    legacy = host.session(organization_id, agent_id, "ses_legacy")
    assert legacy["runtime_type"] == "opencode"
    assert host.stage_agent(envelope) is False
    assert host.agent(organization_id, agent_id)["applied_envelope"] == legacy_envelope_json

    # Reconciliation can rediscover a native OpenCode thread, but never alter
    # its original binding or organization/agent ownership.
    host.save_session(
        organization_id,
        agent_id,
        "ses_legacy",
        "/workspace/legacy",
        "Legacy",
    )
    assert host.session(organization_id, agent_id, "ses_legacy") == legacy
    with pytest.raises(ValueError, match="provenance"):
        host.save_session(
            organization_id,
            agent_id,
            "ses_legacy",
            "/workspace/legacy",
            "Legacy",
            runtime_type="codex",
        )
