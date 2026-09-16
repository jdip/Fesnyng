import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import pytest

from fesnyng_backend.host_dispatch import DispatchStore, Submission
from fesnyng_backend.host_interactions import Interactions
from fesnyng_backend.host_models import Actor, HostAgentConfiguration, WorkspaceExpectation
from fesnyng_backend.host_runtime import DockerRuntime, RuntimeUnavailable, WorkspaceSafetyChanged
from fesnyng_backend.host_store import HostStore
from fesnyng_backend.host_workspace import Workspace, _history_semantics
from fesnyng_backend.host_workspace_lifecycle import WorkspaceLifecycle
from fesnyng_backend.settings import ServiceSettings


class LifecycleNative:
    """Complete runtime seam for lifecycle tests that never need generic workspace work."""

    def lock(self, agent_id):
        return asyncio.Lock()

    async def create_session(self, organization_id, agent_id, title, workspace, **kwargs):
        raise AssertionError("not used by lifecycle tests")

    async def workspace_path(self, organization_id, agent_id, directory, path):
        raise AssertionError("not used by lifecycle tests")

    async def workspace_context(self, organization_id, agent_id, directory):
        raise AssertionError("not used by lifecycle tests")

    async def workspace_metadata_many(self, organization_id, agent_id, directory, paths):
        raise AssertionError("not used by lifecycle tests")

    @asynccontextmanager
    async def workspace_download(self, organization_id, agent_id, directory, path):
        raise AssertionError("not used by lifecycle tests")
        yield None

    async def fork_workspace(self, organization_id, agent_id, source_directory):
        raise AssertionError("not used by lifecycle tests")

    async def request_with_query(self, organization_id, agent_id, path, query, *, directory):
        raise AssertionError("not used by lifecycle tests")

    async def request(
        self, organization_id, agent_id, path, *, method="GET", body=None, directory=None
    ):
        raise AssertionError("not used by lifecycle tests")

    async def workspace_safety(self, organization_id, agent_id, directory):
        raise AssertionError("not used by lifecycle tests")

    async def remove_workspace(
        self, organization_id, agent_id, directory, *, discard, expected_safety_digest
    ):
        raise AssertionError("not used by lifecycle tests")

    async def replace_workspace(
        self,
        organization_id,
        agent_id,
        directory,
        *,
        repository_url=None,
        creation_id=None,
        working_branch=None,
        working_revision=None,
    ):
        raise AssertionError("not used by lifecycle tests")

    async def workspace_repository_matches(
        self, organization_id, agent_id, directory, *, repository_url, creation_id
    ):
        raise AssertionError("not used by lifecycle tests")

    async def workspace_exists(self, organization_id, agent_id, directory):
        raise AssertionError("not used by lifecycle tests")

    async def assert_codex_thread_quiet(self, organization_id, agent_id, thread_id):
        raise AssertionError("not used by lifecycle tests")


def lifecycle_workspace(store, runtime: LifecycleNative, dispatches: DispatchStore) -> Workspace:
    return Workspace(store, runtime, dispatches, Interactions(store, runtime))


def test_completed_workspace_creation_binds_its_mapped_session(tmp_path: Path):
    store = HostStore(
        ServiceSettings(
            service="agent-host",
            database_path=tmp_path / "host.sqlite3",
            state_directory=tmp_path,
        )
    )
    store.initialize()
    organization, agent, creation = str(uuid4()), str(uuid4()), str(uuid4())
    store.bind_organization(organization, "x" * 32)
    envelope = HostAgentConfiguration(
        host_id=store.instance_id,
        organization_id=organization,
        agent_id=agent,
        version=1,
        name="Engineer",
    )
    store.stage_agent(envelope)
    store.mark_applied(envelope)
    directory = f"/workspace/default/threads/{creation}"
    store.reserve_workspace_creation(organization, agent, creation, "a" * 64, directory, None, None)
    store.mark_workspace_prepared(organization, agent, creation, None)
    store.mark_workspace_native_attempted(organization, agent, creation)
    store.save_session(organization, agent, "ses_workspace", directory, "Workspace")
    store.complete_workspace_creation(
        organization,
        agent,
        creation,
        {"id": "ses_workspace", "directory": directory, "title": "Workspace"},
    )

    binding = store.workspace_binding(organization, agent, "ses_workspace")

    assert binding is not None
    assert binding["workspace_id"] == creation
    assert binding["kind"] == "ordinary"
    assert binding["state"] == "ready"
    assert binding["generation"] == 0


def test_workspace_inspection_marks_legacy_paths_unavailable_for_cleanup(tmp_path: Path):
    store = HostStore(
        ServiceSettings(
            service="agent-host", database_path=tmp_path / "host.sqlite3", state_directory=tmp_path
        )
    )
    store.initialize()
    organization, agent = str(uuid4()), str(uuid4())
    store.bind_organization(organization, "x" * 32)
    envelope = HostAgentConfiguration(
        host_id=store.instance_id,
        organization_id=organization,
        agent_id=agent,
        version=1,
        name="Engineer",
    )
    store.stage_agent(envelope)
    store.mark_applied(envelope)
    store.save_session(organization, agent, "ses_legacy", "/workspace/default/legacy", "Legacy")

    inspection = asyncio.run(
        WorkspaceLifecycle(store, object()).inspect(organization, agent, "ses_legacy")
    )

    assert inspection["state"] == "legacy"
    assert inspection["cleanup"]["remove"] == {
        "available": False,
        "reason": "Host ownership was not recorded for this workspace",
    }


def test_workspace_safety_keeps_a_branch_without_upstream_inspectable(tmp_path: Path, monkeypatch):
    runtime = DockerRuntime.__new__(DockerRuntime)

    async def inspect(_org, _agent):
        return {"state": {"Running": True}}

    async def docker(*_args):
        return (
            b"\0".join(
                [
                    b"repository",
                    b"unsafe",
                    b"a" * 64,
                    b"0",
                    b"0",
                    b"0",
                    b"0",
                    b"0",
                    b"revision",
                    b"branch",
                    b"",
                ]
            )
            + b"\0"
        )

    monkeypatch.setattr(runtime, "inspect", inspect)
    monkeypatch.setattr(runtime, "docker", docker)
    monkeypatch.setattr(runtime, "name", lambda _agent: "agent")
    monkeypatch.setattr(runtime, "employee_workspace_root", lambda _org, _agent: tmp_path)

    safety = asyncio.run(runtime.workspace_safety("org", "agent", str(tmp_path / "thread")))

    assert safety["state"] == "unsafe"
    assert safety["ahead"] == 0
    assert safety["upstream"] is None


def _owned_workspace(tmp_path: Path):
    store = HostStore(
        ServiceSettings(
            service="agent-host", database_path=tmp_path / "host.sqlite3", state_directory=tmp_path
        )
    )
    store.initialize()
    organization, agent, creation = str(uuid4()), str(uuid4()), str(uuid4())
    store.bind_organization(organization, "x" * 32)
    envelope = HostAgentConfiguration(
        host_id=store.instance_id,
        organization_id=organization,
        agent_id=agent,
        version=1,
        name="Engineer",
    )
    store.stage_agent(envelope)
    store.mark_applied(envelope)
    directory = f"/workspace/default/threads/{creation}"
    store.reserve_workspace_creation(organization, agent, creation, "a" * 64, directory, None, None)
    store.mark_workspace_prepared(organization, agent, creation, None)
    store.mark_workspace_native_attempted(organization, agent, creation)
    store.save_session(organization, agent, "ses_workspace", directory, "Workspace")
    store.complete_workspace_creation(
        organization,
        agent,
        creation,
        {"id": "ses_workspace", "directory": directory, "title": "Workspace"},
    )
    dispatches = DispatchStore(store)
    dispatches.initialize()
    return store, dispatches, organization, agent, creation


def test_removal_snapshot_closes_dispatch_admission_before_filesystem_mutation(tmp_path: Path):
    store, dispatches, organization, agent, workspace_id = _owned_workspace(tmp_path)
    snapshot = {
        "session": {
            "id": "ses_workspace",
            "directory": f"/workspace/default/threads/{workspace_id}",
        },
        "history": [],
        "children": {},
    }
    store.begin_workspace_removal(
        organization,
        agent,
        workspace_id,
        0,
        "actor",
        snapshot,
        "b" * 64,
        "c" * 64,
    )

    binding = store.workspace_binding(organization, agent, "ses_workspace")
    assert binding is not None
    assert binding["state"] == "removing"
    assert binding["history_digest"] == "b" * 64
    assert binding["history_snapshot"] is not None
    with pytest.raises(RuntimeUnavailable, match="lifecycle operation"):
        dispatches.enqueue(
            organization,
            agent,
            "ses_workspace",
            Submission(id=uuid4(), text="too late"),
            Actor(kind="human", id=uuid4(), name="Member"),
        )


def test_removed_inspection_retains_exact_replace_expectation_and_history(tmp_path: Path):
    store, _dispatches, organization, agent, workspace_id = _owned_workspace(tmp_path)
    directory = f"/workspace/default/threads/{workspace_id}"
    snapshot = {
        "runtime_type": "opencode",
        "session": {"id": "ses_workspace", "directory": directory},
        "history": [],
        "children": {},
    }
    digest = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    store.begin_workspace_removal(
        organization, agent, workspace_id, 0, "actor", snapshot, digest, "c" * 64
    )
    store.complete_workspace_removal(organization, agent, workspace_id)

    inspection = asyncio.run(
        WorkspaceLifecycle(store, object()).inspect(organization, agent, "ses_workspace")
    )

    assert inspection["state"] == "removed"
    assert inspection["generation"] == 1
    assert inspection["safety_digest"] == "c" * 64
    assert inspection["history"] == {"state": "verified"}
    assert inspection["cleanup"]["replace"] == {"available": True}


def test_removal_persists_exact_working_branch_and_revision(tmp_path: Path):
    store, _dispatches, organization, agent, workspace_id = _owned_workspace(tmp_path)
    store.begin_workspace_removal(
        organization,
        agent,
        workspace_id,
        0,
        "actor",
        {"session": {"id": "ses_workspace"}, "history": [], "children": {}},
        "b" * 64,
        "c" * 64,
        working_branch="renamed-work",
        working_revision="d" * 40,
    )

    binding = store.workspace_binding(organization, agent, "ses_workspace")

    assert binding is not None
    assert binding["working_branch"] == "renamed-work"
    assert binding["working_revision"] == "d" * 40


def test_repository_replacement_uses_recorded_branch_and_revision(tmp_path: Path, monkeypatch):
    runtime = DockerRuntime.__new__(DockerRuntime)
    runtime.workspace_root = tmp_path
    organization, agent, creation_id = str(uuid4()), str(uuid4()), str(uuid4())
    calls = []

    async def docker(*args):
        calls.append(args)
        return b""

    monkeypatch.setattr(runtime, "docker", docker)
    monkeypatch.setattr(runtime, "name", lambda _agent: "agent")
    awaitable = runtime.replace_workspace(
        organization,
        agent,
        str(tmp_path / organization / agent / "threads" / "child"),
        repository_url="https://example.invalid/repository.git",
        creation_id=creation_id,
        working_branch="renamed-child-branch",
        working_revision="e" * 40,
    )
    asyncio.run(awaitable)

    assert calls
    assert "refs/heads/$branch" in calls[0][6]
    assert calls[0][-3:] == (
        "renamed-child-branch",
        "e" * 40,
        "https://example.invalid/repository.git",
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda snapshot: {**snapshot, "session": {**snapshot["session"], "id": "foreign"}},
        lambda snapshot: {
            **snapshot,
            "session": {**snapshot["session"], "directory": "/other/employee/thread"},
        },
    ],
)
def test_removed_workspace_rejects_retained_history_with_foreign_native_identity(
    tmp_path: Path, mutate
):
    store, dispatches, organization, agent, workspace_id = _owned_workspace(tmp_path)
    directory = f"/workspace/default/threads/{workspace_id}"
    snapshot = {
        "runtime_type": "opencode",
        "session": {"id": "ses_workspace", "directory": directory},
        "history": [],
        "children": {},
    }
    digest = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    store.begin_workspace_removal(
        organization, agent, workspace_id, 0, "actor", snapshot, digest, "c" * 64
    )
    store.complete_workspace_removal(organization, agent, workspace_id)
    corrupted = mutate(snapshot)
    corrupted_digest = hashlib.sha256(
        json.dumps(corrupted, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with store.connect() as connection:
        connection.execute(
            "UPDATE host_workspace_bindings SET history_snapshot=?,history_digest=? WHERE workspace_id=?",
            (json.dumps(corrupted), corrupted_digest, workspace_id),
        )

    inspection = asyncio.run(
        WorkspaceLifecycle(store, object()).inspect(organization, agent, "ses_workspace")
    )
    assert inspection["history"] == {"state": "unavailable"}
    assert inspection["cleanup"]["replace"]["available"] is False
    with pytest.raises(RuntimeUnavailable, match="history receipt"):
        asyncio.run(
            lifecycle_workspace(store, LifecycleNative(), dispatches).messages(
                organization, agent, "ses_workspace"
            )
        )


def test_removed_workspace_rejects_history_digest_tampering(tmp_path: Path):
    store, dispatches, organization, agent, workspace_id = _owned_workspace(tmp_path)
    directory = f"/workspace/default/threads/{workspace_id}"
    snapshot = {
        "runtime_type": "opencode",
        "session": {"id": "ses_workspace", "directory": directory},
        "history": [],
        "children": {},
    }
    digest = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    store.begin_workspace_removal(
        organization, agent, workspace_id, 0, "actor", snapshot, digest, "c" * 64
    )
    store.complete_workspace_removal(organization, agent, workspace_id)
    with store.connect() as connection:
        connection.execute(
            "UPDATE host_workspace_bindings SET history_digest=? WHERE workspace_id=?",
            ("0" * 64, workspace_id),
        )

    inspection = asyncio.run(
        WorkspaceLifecycle(store, object()).inspect(organization, agent, "ses_workspace")
    )
    assert inspection["history"] == {"state": "unavailable"}
    with pytest.raises(RuntimeUnavailable, match="history receipt"):
        asyncio.run(
            lifecycle_workspace(store, LifecycleNative(), dispatches).messages(
                organization, agent, "ses_workspace"
            )
        )


def test_removal_rejects_dispatch_admitted_while_native_history_is_captured(tmp_path: Path):
    store, dispatches, organization, agent, workspace_id = _owned_workspace(tmp_path)

    class Native(LifecycleNative):
        removed = False

        def lock(self, agent_id):
            return asyncio.Lock()

        async def workspace_safety(self, organization_id, agent_id, directory):
            return {
                "kind": "ordinary",
                "state": "safe",
                "digest": "c" * 64,
                "dirty": 0,
                "untracked": 0,
                "ignored": 0,
                "ahead": 0,
                "branch": None,
                "upstream": None,
            }

        async def request(
            self, organization_id, agent_id, path, *, method="GET", body=None, directory=None
        ):
            if path == "/session/status":
                return {"ses_workspace": {"type": "idle"}}
            if path == "/session/ses_workspace":
                return {"id": "ses_workspace", "directory": directory}
            if path == "/session/ses_workspace/message":
                dispatches.enqueue(
                    organization,
                    agent,
                    "ses_workspace",
                    Submission(id=uuid4(), text="arrived during capture"),
                    Actor(kind="human", id=uuid4(), name="Member"),
                )
                return []
            if path == "/session/ses_workspace/children":
                return []
            raise AssertionError(path)

        async def remove_workspace(
            self, organization_id, agent_id, directory, *, discard, expected_safety_digest
        ):
            self.removed = True

    native = Native()
    workspace = lifecycle_workspace(store, native, dispatches)
    expected = WorkspaceExpectation(workspace_id=workspace_id, generation=0, safety_digest="c" * 64)

    with pytest.raises(ValueError, match="durable delivery"):
        asyncio.run(
            workspace.remove_workspace(
                organization,
                agent,
                "ses_workspace",
                expected,
                discard=False,
                author=Actor(kind="human", id=uuid4(), name="Member"),
            )
        )

    assert native.removed is False
    binding = store.workspace_binding(organization, agent, "ses_workspace")
    assert binding is not None and binding["state"] == "ready"


def test_inspection_treats_sparse_opencode_status_as_idle(tmp_path: Path):
    store, dispatches, organization, agent, _workspace_id = _owned_workspace(tmp_path)

    class Native(LifecycleNative):
        async def workspace_safety(self, organization_id, agent_id, directory):
            return {
                "kind": "ordinary",
                "state": "safe",
                "digest": "c" * 64,
                "dirty": 0,
                "untracked": 0,
                "ignored": 0,
                "ahead": 0,
                "entries": 0,
                "branch": None,
                "upstream": None,
            }

        async def request(
            self, organization_id, agent_id, path, *, method="GET", body=None, directory=None
        ):
            if path == "/session/status":
                return {}
            if path == "/session/ses_workspace/children":
                return []
            raise AssertionError(path)

    inspection = asyncio.run(
        lifecycle_workspace(store, Native(), dispatches).workspace_inspection(
            organization, agent, "ses_workspace"
        )
    )

    assert inspection["cleanup"]["remove"] == {"available": True}


def test_inspection_keeps_ready_binding_when_safety_scan_is_unavailable(tmp_path: Path):
    store, dispatches, organization, agent, _workspace_id = _owned_workspace(tmp_path)

    class Native(LifecycleNative):
        async def workspace_safety(self, organization_id, agent_id, directory):
            raise RuntimeUnavailable("employee container is unavailable")

    inspection = asyncio.run(
        lifecycle_workspace(store, Native(), dispatches).workspace_inspection(
            organization, agent, "ses_workspace"
        )
    )

    assert inspection["state"] == "ready"
    assert inspection["git"] == {"state": "unavailable"}
    assert inspection["history"] == {"state": "unavailable"}
    assert inspection["cleanup"]["remove"]["available"] is False
    binding = store.workspace_binding(organization, agent, "ses_workspace")
    assert binding is not None and binding["state"] == "ready"


def test_history_semantics_inherits_harness_for_native_child_receipts():
    snapshot = {
        "runtime_type": "opencode",
        "session": {"id": "root", "directory": "/managed/root", "updatedAt": 1},
        "history": [],
        "children": {
            "child": {
                "session": {"id": "child", "directory": "/managed/child", "updatedAt": 2},
                "history": [],
                "children": {},
            }
        },
    }

    semantics = _history_semantics(snapshot)

    assert semantics["children"] == [
        {
            "runtime_type": "opencode",
            "id": "child",
            "directory": "/managed/child",
            "history": [],
            "children": [],
        }
    ]


def test_managed_fork_inherits_source_creation_provenance(tmp_path: Path):
    store, _dispatches, organization, agent, workspace_id = _owned_workspace(tmp_path)
    source = store.workspace_binding(organization, agent, "ses_workspace")
    assert source is not None
    destination = source["directory"] + "-fork-isolated"
    store.save_session(organization, agent, "ses_fork", destination, "Fork")

    store.register_workspace_fork(organization, agent, "ses_workspace", "ses_fork", destination)

    fork = store.workspace_binding(organization, agent, "ses_fork")
    assert fork is not None
    assert fork["kind"] == "fork"
    assert fork["creation_id"] == workspace_id


def test_predelete_runtime_refusal_reopens_verified_existing_workspace(tmp_path: Path):
    store, dispatches, organization, agent, workspace_id = _owned_workspace(tmp_path)

    class Native(LifecycleNative):
        attempts = 0

        def lock(self, agent_id):
            return asyncio.Lock()

        async def workspace_safety(self, organization_id, agent_id, directory):
            return {
                "kind": "ordinary",
                "state": "safe",
                "digest": "c" * 64,
                "dirty": 0,
                "untracked": 0,
                "ignored": 0,
                "ahead": 0,
                "entries": 0,
                "revision": None,
                "branch": None,
                "upstream": None,
            }

        async def request(
            self, organization_id, agent_id, path, *, method="GET", body=None, directory=None
        ):
            if path == "/session/status":
                return {}
            if path == "/session/ses_workspace":
                return {"id": "ses_workspace", "directory": directory}
            if path == "/session/ses_workspace/message":
                return []
            if path == "/session/ses_workspace/children":
                return []
            raise AssertionError(path)

        async def remove_workspace(
            self, organization_id, agent_id, directory, *, discard, expected_safety_digest
        ):
            self.attempts += 1
            if self.attempts == 1:
                raise WorkspaceSafetyChanged(
                    "Workspace safety changed; inspect again before cleanup"
                )

    native = Native()
    expected = WorkspaceExpectation(workspace_id=workspace_id, generation=0, safety_digest="c" * 64)
    with pytest.raises(WorkspaceSafetyChanged, match="safety changed"):
        asyncio.run(
            lifecycle_workspace(store, native, dispatches).remove_workspace(
                organization,
                agent,
                "ses_workspace",
                expected,
                discard=False,
                author=Actor(kind="human", id=uuid4(), name="Member"),
            )
        )
    binding = store.workspace_binding(organization, agent, "ses_workspace")
    assert binding is not None and binding["state"] == "ready"
    removed = asyncio.run(
        lifecycle_workspace(store, native, dispatches).remove_workspace(
            organization,
            agent,
            "ses_workspace",
            expected,
            discard=True,
            author=Actor(kind="human", id=uuid4(), name="Member"),
        )
    )
    assert native.attempts == 2
    assert removed["state"] == "removed"


def test_partial_delete_evidence_keeps_workspace_unavailable(tmp_path: Path):
    store, dispatches, organization, agent, workspace_id = _owned_workspace(tmp_path)

    class Native(LifecycleNative):
        safety_reads = 0

        def lock(self, agent_id):
            return asyncio.Lock()

        async def workspace_safety(self, organization_id, agent_id, directory):
            self.safety_reads += 1
            return {
                "kind": "ordinary",
                "state": "safe",
                "digest": "c" * 64 if self.safety_reads == 1 else "d" * 64,
                "dirty": 0,
                "untracked": 0,
                "ignored": 0,
                "ahead": 0,
                "entries": 0,
                "revision": None,
                "branch": None,
                "upstream": None,
            }

        async def request(
            self, organization_id, agent_id, path, *, method="GET", body=None, directory=None
        ):
            if path == "/session/status":
                return {}
            if path == "/session/ses_workspace":
                return {"id": "ses_workspace", "directory": directory}
            if path in {"/session/ses_workspace/message", "/session/ses_workspace/children"}:
                return []
            raise AssertionError(path)

        async def remove_workspace(
            self, organization_id, agent_id, directory, *, discard, expected_safety_digest
        ):
            raise RuntimeUnavailable("filesystem operation interrupted")

    with pytest.raises(RuntimeUnavailable, match="interrupted"):
        asyncio.run(
            lifecycle_workspace(store, Native(), dispatches).remove_workspace(
                organization,
                agent,
                "ses_workspace",
                WorkspaceExpectation(
                    workspace_id=workspace_id, generation=0, safety_digest="c" * 64
                ),
                discard=False,
                author=Actor(kind="human", id=uuid4(), name="Member"),
            )
        )
    binding = store.workspace_binding(organization, agent, "ses_workspace")
    assert binding is not None and binding["state"] == "unavailable"


def test_removed_opencode_child_history_reads_without_live_native_tree(tmp_path: Path):
    store, dispatches, organization, agent, workspace_id = _owned_workspace(tmp_path)
    directory = f"/workspace/default/threads/{workspace_id}"
    snapshot = {
        "runtime_type": "opencode",
        "session": {"id": "ses_workspace", "directory": directory},
        "history": [],
        "children": {
            "child": {
                "session": {"id": "ses_child", "directory": directory + "-child"},
                "history": [{"info": {"sessionID": "ses_child"}, "parts": []}],
                "children": {},
            }
        },
    }
    digest = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    store.begin_workspace_removal(
        organization, agent, workspace_id, 0, "actor", snapshot, digest, "c" * 64
    )
    store.complete_workspace_removal(organization, agent, workspace_id)

    history = asyncio.run(
        lifecycle_workspace(store, LifecycleNative(), dispatches).messages(
            organization, agent, "ses_child"
        )
    )

    assert history == [{"info": {"sessionID": "ses_child"}, "parts": []}]


def test_replacement_accepts_unchanged_history_despite_volatile_native_metadata(tmp_path: Path):
    store, dispatches, organization, agent, workspace_id = _owned_workspace(tmp_path)
    directory = f"/workspace/default/threads/{workspace_id}"
    retained = {
        "runtime_type": "opencode",
        "session": {"id": "ses_workspace", "directory": directory, "updatedAt": 1},
        "history": [],
        "children": {},
    }
    digest = hashlib.sha256(
        json.dumps(retained, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    store.begin_workspace_removal(
        organization, agent, workspace_id, 0, "actor", retained, digest, "c" * 64
    )
    store.complete_workspace_removal(organization, agent, workspace_id)

    class Native(LifecycleNative):
        def lock(self, agent_id):
            return asyncio.Lock()

        async def replace_workspace(
            self,
            organization_id,
            agent_id,
            directory,
            *,
            repository_url=None,
            creation_id=None,
            working_branch=None,
            working_revision=None,
        ):
            return None

        async def workspace_safety(self, organization_id, agent_id, directory):
            return {
                "kind": "ordinary",
                "state": "safe",
                "digest": "d" * 64,
                "dirty": 0,
                "untracked": 0,
                "ignored": 0,
                "ahead": 0,
                "entries": 0,
                "branch": None,
                "upstream": None,
            }

        async def request(
            self, organization_id, agent_id, path, *, method="GET", body=None, directory=None
        ):
            if path == "/session/status":
                return {"ses_workspace": {"type": "idle"}}
            if path == "/session/ses_workspace":
                return {"id": "ses_workspace", "directory": directory, "updatedAt": 2}
            if path == "/session/ses_workspace/message":
                return []
            if path == "/session/ses_workspace/children":
                return []
            raise AssertionError(path)

    result = asyncio.run(
        lifecycle_workspace(store, Native(), dispatches).replace_workspace(
            organization,
            agent,
            "ses_workspace",
            WorkspaceExpectation(workspace_id=workspace_id, generation=1, safety_digest="c" * 64),
            author=Actor(kind="human", id=uuid4(), name="Member"),
        )
    )

    assert result["state"] == "ready"
    binding = store.workspace_binding(organization, agent, "ses_workspace")
    assert binding is not None and binding["state"] == "ready" and binding["generation"] == 2
