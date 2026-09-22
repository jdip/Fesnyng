import json
from uuid import uuid4

import pytest

from fesnyng_backend.agent_storage import AgentStore
from fesnyng_backend.control_store import ControlPlaneStore


def test_agent_configuration_retains_identity_and_version_after_restart(organization):
    settings, _, owner, org, agents, host_id = organization
    agent = agents.create_agent(org.id, owner.id, {"name": "Engineer", "host_id": host_id})
    reopened = ControlPlaneStore(settings.database_path)
    reopened.initialize()
    assert AgentStore(reopened).get_agent(org.id, agent["id"]) == agent
    assert agent["desired_version"] == 1
    assert agent["applied_version"] is None
    assert agent["configuration_status"] == "pending"


def test_legacy_agent_configuration_defaults_to_opencode_without_a_configuration_revision(
    organization,
):
    _, control, owner, org, agents, host_id = organization
    agent = agents.create_agent(org.id, owner.id, {"name": "Engineer", "host_id": host_id})
    legacy = dict(agent["configuration"])
    del legacy["runtime_type"]
    with control.connect() as connection:
        connection.execute(
            "UPDATE agent_configurations SET configuration=? WHERE agent_id=? AND version=1",
            (json.dumps(legacy), agent["id"]),
        )

    restored = agents.get_agent(org.id, agent["id"])

    assert restored["desired_version"] == 1
    assert restored["configuration"]["runtime_type"] == "opencode"


def test_configuration_update_is_versioned_and_rejects_a_stale_edit(organization):
    _, _, owner, org, agents, host_id = organization
    agent = agents.create_agent(org.id, owner.id, {"name": "Engineer", "host_id": host_id})
    updated = agents.update_agent(
        org.id,
        agent["id"],
        owner.id,
        {
            "expected_version": 1,
            "configuration": {"instructions": "Review changes carefully."},
        },
    )
    assert updated["id"] == agent["id"]
    assert updated["desired_version"] == 2
    assert updated["applied_version"] is None
    assert updated["configuration"]["instructions"] == "Review changes carefully."
    with pytest.raises(ValueError, match="version conflict"):
        agents.update_agent(org.id, agent["id"], owner.id, {"expected_version": 1, "name": "Stale"})
    assert agents.get_agent(org.id, agent["id"]) == updated


@pytest.mark.parametrize("runtime_type,replacement", [("opencode", "codex"), ("codex", "opencode")])
def test_ordinary_settings_cannot_switch_an_existing_employee_harness(
    organization, runtime_type, replacement
):
    _, _, owner, org, agents, host_id = organization
    agent = agents.create_agent(
        org.id,
        owner.id,
        {"name": "Engineer", "host_id": host_id, "configuration": {"runtime_type": runtime_type}},
    )
    with pytest.raises(ValueError, match="harness switch"):
        agents.update_agent(
            org.id,
            agent["id"],
            owner.id,
            {"expected_version": 1, "configuration": {"runtime_type": replacement}},
        )
    assert agents.get_agent(org.id, agent["id"]) == agent


def test_durable_harness_switch_intent_blocks_ordinary_updates_until_freeze_commits(organization):
    _, _, owner, org, agents, host_id = organization
    agent = agents.create_agent(org.id, owner.id, {"name": "Engineer", "host_id": host_id})

    intent = agents.begin_harness_switch(org.id, agent["id"], owner.id, 1, "codex")

    assert intent["state"] == "capturing"
    assert agents.get_agent(org.id, agent["id"])["configuration"]["runtime_type"] == "opencode"
    with pytest.raises(ValueError, match="switch is in progress"):
        agents.update_agent(
            org.id, agent["id"], owner.id, {"expected_version": 1, "name": "Changed"}
        )

    agents.mark_harness_switch_frozen(org.id, agent["id"], 1, "codex")
    switched = agents.commit_harness_switch(org.id, agent["id"], owner.id, 1, "codex")

    assert switched["desired_version"] == 2
    assert switched["applied_version"] is None
    assert switched["configuration"]["runtime_type"] == "codex"
    assert agents.harness_switch(org.id, agent["id"]) is None


def test_assignment_and_reporting_cannot_cross_organizations_or_create_cycles(organization):
    _, control, owner, org, agents, host_id = organization
    other_org = control.create_organization(owner.id, "Separate organization")
    with pytest.raises(ValueError, match="Host is not allocated"):
        agents.create_agent(other_org.id, owner.id, {"name": "Foreign", "host_id": host_id})
    chief = agents.create_agent(org.id, owner.id, {"name": "Chief", "host_id": host_id})
    junior = agents.create_agent(
        org.id,
        owner.id,
        {
            "name": "Junior",
            "host_id": host_id,
            "reports_to_agent_id": chief["id"],
        },
    )
    assert junior["reports_to_agent_id"] == chief["id"]
    with pytest.raises(ValueError, match="cycle"):
        agents.update_agent(
            org.id,
            chief["id"],
            owner.id,
            {
                "expected_version": 1,
                "reports_to_agent_id": junior["id"],
            },
        )
    with pytest.raises(LookupError):
        agents.get_agent(other_org.id, chief["id"])
    with pytest.raises(ValueError, match="Reporting agent not found"):
        agents.update_agent(
            org.id,
            junior["id"],
            owner.id,
            {
                "expected_version": 1,
                "reports_to_agent_id": str(uuid4()),
            },
        )


def test_agent_defaults_and_configuration_input_limits(organization):
    _, _, owner, org, agents, host_id = organization
    agent = agents.create_agent(org.id, owner.id, {"name": "Engineer", "host_id": host_id})
    assert agent["configuration"]["execution_type"] == "docker"
    assert agent["configuration"]["runtime_type"] == "opencode"
    assert agent["configuration"]["model"] == "gpt-6-astra"
    assert agent["configuration"]["reasoning_effort"] is None
    with pytest.raises(ValueError):
        agents.create_agent(
            org.id,
            owner.id,
            {
                "name": "Unsafe path",
                "host_id": host_id,
                "configuration": {"workspace": "../../other"},
            },
        )


def test_agent_configuration_retains_the_optional_reasoning_effort(organization):
    _, _, owner, org, agents, host_id = organization
    agent = agents.create_agent(
        org.id,
        owner.id,
        {
            "name": "Engineer",
            "host_id": host_id,
            "configuration": {"runtime_type": "codex", "reasoning_effort": "high"},
        },
    )

    assert agent["configuration"]["reasoning_effort"] == "high"
    with pytest.raises(ValueError):
        agents.update_agent(
            org.id, agent["id"], owner.id, {"expected_version": 1, "applied_version": 1}
        )
    with pytest.raises(ValueError):
        agents.update_agent(
            org.id, agent["id"], owner.id, {"expected_version": 1, "host_id": str(uuid4())}
        )


def test_profile_assignments_are_scoped_to_the_agent_organization(organization):
    _, control, owner, org, agents, host_id = organization
    other_org = control.create_organization(owner.id, "Separate organization")
    profile = agents.create_profile(org.id, owner.id, {"name": "Shared subscription"})
    foreign = agents.create_profile(other_org.id, owner.id, {"name": "Separate subscription"})
    agent = agents.create_agent(
        org.id,
        owner.id,
        {
            "name": "Engineer",
            "host_id": host_id,
            "configuration": {"profile_id": profile["id"]},
        },
    )
    assert agent["configuration"]["profile_id"] == profile["id"]
    with pytest.raises(ValueError, match="Credential profile not found"):
        agents.update_agent(
            org.id,
            agent["id"],
            owner.id,
            {
                "expected_version": 1,
                "configuration": {"profile_id": foreign["id"]},
            },
        )


def test_policy_defaults_to_broad_access_and_preserves_versioned_limits(organization):
    _, _, owner, org, agents, _ = organization
    policy = agents.get_policy(org.id)
    assert policy["configuration"]["default_permission"] == "allow"
    assert policy["configuration"]["allow_thread_overrides"] is True
    updated = agents.update_policy(
        org.id,
        owner.id,
        {
            "expected_version": policy["desired_version"],
            "configuration": {
                "mandatory_permissions": [
                    {"permission": "bash", "pattern": "rm -rf *", "action": "deny"},
                ]
            },
        },
    )
    assert updated["desired_version"] == policy["desired_version"] + 1
    assert updated["configuration"]["mandatory_permissions"][0]["action"] == "deny"
    with pytest.raises(ValueError, match="version conflict"):
        agents.update_policy(org.id, owner.id, {"expected_version": 1, "configuration": {}})
    assert agents.get_policy(org.id) == updated


def test_host_acknowledgement_cannot_apply_foreign_or_future_configuration(organization):
    _, _, owner, org, agents, host_id = organization
    agent = agents.create_agent(org.id, owner.id, {"name": "Engineer", "host_id": host_id})
    with pytest.raises(ValueError):
        agents.acknowledge_host(org.id, agent["id"], str(uuid4()), 1)
    with pytest.raises(ValueError):
        agents.acknowledge_host(org.id, agent["id"], host_id, 2)
    agents.update_agent(org.id, agent["id"], owner.id, {"expected_version": 1, "name": "Updated"})
    agents.acknowledge_host(org.id, agent["id"], host_id, 1)
    assert agents.get_agent(org.id, agent["id"])["configuration_status"] == "pending"
    agents.acknowledge_host(org.id, agent["id"], host_id, 2)
    assert agents.get_agent(org.id, agent["id"])["configuration_status"] == "applied"
    agents.acknowledge_host(org.id, agent["id"], host_id, 1)
    assert agents.get_agent(org.id, agent["id"])["applied_version"] == 2
