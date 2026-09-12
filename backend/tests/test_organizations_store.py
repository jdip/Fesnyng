from pathlib import Path

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

    import pytest

    with pytest.raises(ValueError, match="retain an owner"):
        store.remove_member(organization.id, owner.id, actor_id=owner.id)
