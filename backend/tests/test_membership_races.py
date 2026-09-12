import pytest


def test_membership_mutations_recheck_authority_after_role_changes(organization):
    _, store, owner, org, _, _ = organization
    admin = store.add_member(
        org.id, "admin", "Admin", "an admin password", "admin", actor_id=owner.id
    )
    target = store.add_member(
        org.id, "target", "Target", "a target password", "member", actor_id=owner.id
    )
    # Simulate an HTTP precheck made while target was still a member.
    assert store.membership_for(admin.user_id, org.id).role == "admin"
    assert store.membership_for(target.user_id, org.id).role == "member"
    store.add_member(org.id, "target", "Target", None, "owner", actor_id=owner.id)
    with pytest.raises(PermissionError):
        store.add_member(org.id, "target", "Target", None, "member", actor_id=admin.user_id)
    with pytest.raises(PermissionError):
        store.remove_member(org.id, target.user_id, actor_id=admin.user_id)
    store.remove_member(org.id, admin.user_id, actor_id=owner.id)
    with pytest.raises(LookupError):
        store.add_member(
            org.id,
            "unpermitted",
            "Unpermitted",
            "a new user password",
            "member",
            actor_id=admin.user_id,
        )
    assert store.membership_for(target.user_id, org.id).role == "owner"
