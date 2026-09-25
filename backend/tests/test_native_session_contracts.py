"""Preserve the caller policies around shared native child validation."""

import asyncio
import secrets
from typing import Any
from uuid import uuid4

import pytest

from fesnyng_backend.agent_host import create_app
from fesnyng_backend.host_models import HostAgentConfiguration
from fesnyng_backend.host_runtime import RuntimeUnavailable
from fesnyng_backend.host_workspace import Workspace
from fesnyng_backend.settings import ServiceSettings

OWNERS = ("listing", "lookup", "count", "policy", "lifecycle")


@pytest.fixture
def tree(tmp_path, monkeypatch):
    app = create_app(
        ServiceSettings(
            service="agent-host", state_directory=tmp_path, database_path=tmp_path / "host.sqlite3"
        )
    )
    store = app.state.host_store
    org, agent = str(uuid4()), str(uuid4())
    store.bind_organization(org, secrets.token_urlsafe(32))
    envelope = HostAgentConfiguration(
        host_id=store.instance_id,
        organization_id=org,
        agent_id=agent,
        version=1,
        name="Tree contract",
    )
    store.stage_agent(envelope)
    store.mark_applied(envelope)
    store.save_session(org, agent, "ses_root", "/root", "Root")
    children: dict[str, Any] = {}
    calls = []

    async def request(organization_id, agent_id, path, *, method="GET", body=None, directory=None):
        assert (organization_id, agent_id, method, body) == (org, agent, "GET", None)
        assert path.startswith("/session/") and path.endswith("/children")
        parent = path.split("/")[2]
        calls.append((parent, directory))
        return children.get(parent, [])

    monkeypatch.setattr(app.state.host_runtime, "request", request)
    workspace = Workspace(
        store, app.state.host_runtime, app.state.dispatch_store, app.state.interactions
    )
    return app, workspace, org, agent, children, calls


def walk(owner, tree, target="ses_target"):
    app, workspace, org, agent, _, _ = tree
    root = app.state.host_store.session(org, agent, "ses_root")
    if owner == "listing":
        operation = workspace._scoped_sessions(org, agent)
    elif owner == "lookup":
        operation = workspace._scoped_session(org, agent, target)
    elif owner == "count":
        operation = workspace._child_count(org, agent, root)
    elif owner == "policy":
        operation = app.state.interactions._session_family(org, agent, "ses_root", "/root")
    else:
        mapped = {s["session_id"]: s for s in app.state.host_store.sessions(org, agent)}
        operation = app.state.dispatcher._session_family(org, agent, root, mapped)
    return asyncio.run(operation)


def child(child_id="ses_child", parent="ses_root", directory="/child"):
    return {"id": child_id, "parentID": parent, "directory": directory}


@pytest.mark.parametrize("owner", OWNERS)
@pytest.mark.parametrize(
    "receipt",
    [
        None,
        {},
        [None],
        [child(child_id="bad/path")],
        [child(parent="ses_unrelated")],
        [child(directory=None)],
    ],
)
def test_every_owner_rejects_malformed_native_children(tree, owner, receipt):
    tree[4]["ses_root"] = receipt
    with pytest.raises(RuntimeUnavailable):
        walk(owner, tree)


@pytest.mark.parametrize("owner", OWNERS)
@pytest.mark.parametrize("cycle", [False, True])
def test_every_owner_rejects_duplicate_or_cyclic_descendants(tree, owner, cycle):
    tree[4]["ses_root"] = [child()] if cycle else [child(), child()]
    if cycle:
        tree[4]["ses_child"] = [child("ses_grandchild", "ses_child")]
        tree[4]["ses_grandchild"] = [child("ses_child", "ses_grandchild")]
    with pytest.raises(RuntimeUnavailable):
        walk(owner, tree)


@pytest.mark.parametrize("owner", OWNERS)
def test_callers_preserve_empty_directory_policy(tree, owner):
    tree[4]["ses_root"] = [child(directory="")]
    if owner in {"policy", "lifecycle"}:
        with pytest.raises(RuntimeUnavailable):
            walk(owner, tree)
    else:
        walk(owner, tree, target="ses_child")
        if owner != "lookup":
            assert ("ses_child", "") in tree[5]


@pytest.mark.parametrize("owner", OWNERS)
def test_callers_preserve_repeated_mapped_root_policy(tree, owner):
    app, _, org, agent, children, _ = tree
    app.state.host_store.save_session(org, agent, "ses_child", "/child", "Mapped fork")
    children["ses_root"] = [child(), child()]
    if owner == "listing":
        assert len(walk(owner, tree)) == 2
    elif owner == "lookup":
        with pytest.raises(LookupError):
            walk(owner, tree)
    else:
        with pytest.raises(RuntimeUnavailable):
            walk(owner, tree)


@pytest.mark.parametrize("owner", ["listing", "lookup", "lifecycle"])
def test_mapped_root_directory_mismatch_is_unavailable(tree, owner):
    app, _, org, agent, children, _ = tree
    app.state.host_store.save_session(org, agent, "ses_child", "/mapped", "Mapped fork")
    children["ses_root"] = [child()]
    with pytest.raises(RuntimeUnavailable):
        walk(owner, tree)


def test_lookup_short_circuits_before_later_invalid_receipts(tree):
    tree[4]["ses_root"] = [child(), None]
    result = walk("lookup", tree, target="ses_child")
    assert (result["session_id"], result["root_session_id"], result["directory"]) == (
        "ses_child",
        "ses_root",
        "/child",
    )
    assert tree[5] == [("ses_root", "/root")]
    with pytest.raises(RuntimeUnavailable):
        walk("listing", tree)


@pytest.mark.parametrize("owner", ["listing", "count", "policy", "lifecycle"])
def test_traversal_keeps_native_sibling_and_lifo_directory_order(tree, owner):
    tree[4]["ses_root"] = [
        child("ses_first", directory="/first"),
        child("ses_second", directory="/second"),
    ]
    result = walk(owner, tree)
    assert tree[5] == [("ses_root", "/root"), ("ses_second", "/second"), ("ses_first", "/first")]
    if owner == "count":
        assert result == 2
    elif owner in {"policy", "lifecycle"}:
        assert result == [("ses_root", "/root"), ("ses_first", "/first"), ("ses_second", "/second")]
    else:
        assert [row["session_id"] for row in result] == ["ses_root", "ses_first", "ses_second"]
