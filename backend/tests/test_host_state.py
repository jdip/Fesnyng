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
