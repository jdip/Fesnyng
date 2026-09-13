from pathlib import Path

import pytest

from fesnyng_backend.control_store import ControlPlaneStore


def test_creating_an_organization_makes_its_creator_an_owner(tmp_path: Path):
    store = ControlPlaneStore(tmp_path / "control-plane.sqlite3")
    store.initialize()
    owner = store.bootstrap_owner("owner", "Initial Owner", "a-long-test-password")

    organization = store.create_organization(owner.id, "Example Organization")

    assert organization.name == "Example Organization"
    assert store.membership_for(owner.id, organization.id).role == "owner"


def test_organization_cannot_remove_its_last_owner(tmp_path: Path):
    store = ControlPlaneStore(tmp_path / "control-plane.sqlite3")
    store.initialize()
    owner = store.bootstrap_owner("owner", "Initial Owner", "a-long-test-password")
    organization = store.create_organization(owner.id, "Example Organization")

    with pytest.raises(ValueError, match="retain an owner"):
        store.remove_member(organization.id, owner.id, actor_id=owner.id)


def test_removing_member_removes_their_thread_pins_and_keeps_other_members_pins(organization):
    _, store, owner, org, agents, host_id = organization
    agent = agents.create_agent(org.id, owner.id, {"name": "Workspace", "host_id": host_id})
    member = store.add_member(
        org.id,
        "member",
        "Member",
        "correct horse battery staple",
        "member",
        actor_id=owner.id,
    )
    store.pin_thread(org.id, owner.id, agent["id"], "ses_owner")
    store.pin_thread(org.id, member.user_id, agent["id"], "ses_member")

    store.remove_member(org.id, member.user_id, actor_id=owner.id)

    with pytest.raises(LookupError):
        store.membership_for(member.user_id, org.id)
    assert store.list_thread_pins(org.id, member.user_id, agent["id"]) == []
    assert store.list_thread_pins(org.id, owner.id, agent["id"]) == ["ses_owner"]
