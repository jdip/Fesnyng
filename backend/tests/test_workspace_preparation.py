import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from fesnyng_backend.codex_runtime import CodexRuntime
from fesnyng_backend.host_models import HostAgentConfiguration
from fesnyng_backend.host_runtime import (
    DockerRuntime,
    RuntimeUnavailable,
    WorkspaceCreationUncertain,
    WorkspacePreparationFailed,
)
from fesnyng_backend.host_store import HostStore
from fesnyng_backend.settings import ServiceSettings
from fesnyng_backend.workspace_preparation import (
    GIT_PREPARE_SCRIPT,
    GIT_PREPARED_RECONCILE_SCRIPT,
    STANDALONE_GIT_FORK_SCRIPT,
    workspace_paths,
)


def test_host_workspace_root_is_absolute_and_employee_scoped(tmp_path: Path):
    root = tmp_path / "host-workspaces"
    settings = ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "state" / "host.sqlite3",
        state_directory=tmp_path / "state",
        workspace_root=root,
    )
    organization, agent, creation = (str(uuid4()), str(uuid4()), str(uuid4()))

    location = workspace_paths(
        settings.agent_workspace_root,
        organization,
        agent,
        creation,
        "https://git.example.test/org/repository.git",
    )

    assert location.employee_root == root.resolve() / organization / agent
    assert location.directory == location.employee_root / "threads" / creation
    assert location.bare_repository is not None
    assert location.bare_repository.parent == location.employee_root / "repositories"
    assert "repository" not in str(location.bare_repository)


def test_workspace_root_rejects_relative_paths(tmp_path: Path):
    with pytest.raises(ValueError, match="absolute"):
        ServiceSettings(
            service="agent-host",
            database_path=tmp_path / "host.sqlite3",
            state_directory=tmp_path / "state",
            workspace_root=Path("relative-workspaces"),
        )


def test_workspace_creation_reservation_returns_only_the_matching_completed_receipt(tmp_path: Path):
    settings = ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "state" / "host.sqlite3",
        state_directory=tmp_path / "state",
    )
    store = HostStore(settings)
    store.initialize()
    organization, agent, creation = (str(uuid4()), str(uuid4()), str(uuid4()))
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
    fingerprint = "a" * 64
    directory = f"/private/workspaces/{organization}/{agent}/threads/{creation}"

    reserved = store.reserve_workspace_creation(
        organization,
        agent,
        creation,
        fingerprint,
        directory,
        None,
        None,
        project_id="project-original",
        requested_checkout_branch="release",
    )
    assert reserved["state"] == "reserved"
    assert reserved["project_id"] == "project-original"
    assert reserved["requested_checkout_branch"] == "release"
    store.mark_workspace_prepared(organization, agent, creation, None)
    store.mark_workspace_native_attempted(organization, agent, creation)
    receipt = {"id": "ses_created", "title": "Thread", "directory": directory}
    store.complete_workspace_creation(organization, agent, creation, receipt)

    assert (
        store.reserve_workspace_creation(
            organization,
            agent,
            creation,
            fingerprint,
            directory,
            None,
            None,
            project_id="project-original",
            requested_checkout_branch="release",
        )["native_receipt"]
        == '{"id":"ses_created","title":"Thread","directory":"' + directory + '"}'
    )
    with pytest.raises(ValueError, match="original request"):
        store.reserve_workspace_creation(
            organization, agent, creation, "b" * 64, directory, None, None
        )


def test_invalid_repository_checkout_is_a_typed_pre_native_failure(tmp_path: Path):
    settings = ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "state" / "host.sqlite3",
        state_directory=tmp_path / "state",
        workspace_root=tmp_path / "workspaces",
    )
    store = HostStore(settings)
    store.initialize()
    organization, agent, creation = (str(uuid4()), str(uuid4()), str(uuid4()))
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
    runtime = DockerRuntime(store, "http://host.invalid")

    with pytest.raises(WorkspacePreparationFailed) as error:
        asyncio.run(
            runtime.create_session(
                organization,
                agent,
                "Invalid branch",
                "default",
                creation_id=creation,
                repository_url="https://git.example.test/repository.git",
                checkout_branch="--invalid",
            )
        )

    assert error.value.as_response() == {
        "code": "workspace_preparation_failed",
        "creation_id": creation,
        "detail": "Checkout branch is not valid",
    }
    with pytest.raises(LookupError):
        store.workspace_creation(organization, agent, creation)


def test_managed_ordinary_workspace_fork_uses_the_safe_copy_path(tmp_path: Path, monkeypatch):
    settings = ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "state" / "host.sqlite3",
        state_directory=tmp_path / "state",
        workspace_root=tmp_path / "workspaces",
    )
    runtime = DockerRuntime(HostStore(settings), "http://host.invalid")
    organization, agent = str(uuid4()), str(uuid4())
    source = str(runtime.employee_workspace_root(organization, agent) / "threads" / "ordinary")
    calls: list[tuple[str, ...]] = []

    async def inspect(_organization: str, _agent: str):
        return {"state": {"Running": True}}

    async def docker(*args: str, **_kwargs: object) -> bytes:
        calls.append(args)
        return b"ordinary" if "fork-workspace-kind" in args else b""

    monkeypatch.setattr(runtime, "inspect", inspect)
    monkeypatch.setattr(runtime, "docker", docker)

    destination = asyncio.run(runtime.fork_workspace(organization, agent, source))

    assert destination.startswith(source + "-fork-")
    copy = calls[-1]
    assert 'find "$1" -name .git -print -quit' in copy[4]


def _codex_envelope(store: HostStore, organization: str, agent: str) -> HostAgentConfiguration:
    envelope = HostAgentConfiguration(
        host_id=store.instance_id,
        organization_id=organization,
        agent_id=agent,
        version=1,
        name="Engineer",
        configuration={"runtime_type": "codex"},
    )
    store.stage_agent(envelope)
    store.mark_applied(envelope)
    return envelope


def _codex_receipt(thread_id: str, directory: str) -> dict[str, object]:
    return {
        "thread": {"id": thread_id, "cwd": directory},
        "cwd": directory,
        "approvalPolicy": "never",
        "approvalsReviewer": "user",
        "sandbox": {"type": "dangerFullAccess"},
    }


def _codex_runtime_for_context(transport: object) -> CodexRuntime:
    runtime = CodexRuntime.__new__(CodexRuntime)
    runtime.transport = cast(Any, transport)
    runtime.resumed_connections = {}
    runtime.started_policies = {}
    return runtime


def test_codex_context_initialization_is_developer_only_and_verifies_resume(tmp_path: Path):
    settings = ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "state" / "host.sqlite3",
        state_directory=tmp_path / "state",
    )
    store = HostStore(settings)
    store.initialize()
    organization, agent = str(uuid4()), str(uuid4())
    store.bind_organization(organization, "x" * 32)
    envelope = _codex_envelope(store, organization, agent)
    directory = "/workspace/default/threads/context"

    class Transport:
        def __init__(self):
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def call(self, _org, _agent, method, params):
            self.calls.append((method, dict(params)))
            if method == "thread/inject_items":
                return {}
            if method == "thread/resume":
                return _codex_receipt("thr_context", directory)
            raise AssertionError(method)

        async def connection_id(self, _org, _agent):
            return "connection"

    transport = Transport()
    runtime = _codex_runtime_for_context(transport)

    asyncio.run(
        runtime.initialize_workspace_context(
            organization, agent, "thr_context", directory, envelope
        )
    )

    assert [method for method, _params in transport.calls] == [
        "thread/inject_items",
        "thread/resume",
    ]
    item = transport.calls[0][1]["items"]
    assert item == [
        {
            "type": "message",
            "role": "developer",
            "content": [
                {
                    "type": "input_text",
                    "text": "The host prepared this thread workspace. No user task has been submitted.",
                }
            ],
        }
    ]
    assert runtime.resumed_connections == {(organization, agent, "thr_context"): "connection"}
    assert runtime.started_policies == {}


def test_codex_context_recovery_resumes_before_injecting_and_never_duplicates(tmp_path: Path):
    settings = ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "state" / "host.sqlite3",
        state_directory=tmp_path / "state",
    )
    store = HostStore(settings)
    store.initialize()
    organization, agent = str(uuid4()), str(uuid4())
    store.bind_organization(organization, "x" * 32)
    envelope = _codex_envelope(store, organization, agent)
    directory = "/workspace/default/threads/context"

    class Transport:
        def __init__(self, *, has_context: bool):
            self.has_context = has_context
            self.calls: list[str] = []

        async def call(self, _org, _agent, method, _params):
            self.calls.append(method)
            if method == "thread/resume" and not self.has_context:
                raise RuntimeUnavailable("no rollout found for thread id thr_context")
            if method == "thread/inject_items":
                self.has_context = True
                return {}
            if method == "thread/resume":
                return _codex_receipt("thr_context", directory)
            raise AssertionError(method)

        async def connection_id(self, _org, _agent):
            return "connection"

    missing = Transport(has_context=False)
    asyncio.run(
        _codex_runtime_for_context(missing).recover_workspace_context(
            organization, agent, "thr_context", directory, envelope
        )
    )
    assert missing.calls == ["thread/resume", "thread/inject_items", "thread/resume"]

    already_initialized = Transport(has_context=True)
    asyncio.run(
        _codex_runtime_for_context(already_initialized).recover_workspace_context(
            organization, agent, "thr_context", directory, envelope
        )
    )
    assert already_initialized.calls == ["thread/resume"]


def test_codex_context_recovery_only_injects_after_the_exact_no_rollout_receipt(tmp_path: Path):
    settings = ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "state" / "host.sqlite3",
        state_directory=tmp_path / "state",
    )
    store = HostStore(settings)
    store.initialize()
    organization, agent = str(uuid4()), str(uuid4())
    store.bind_organization(organization, "x" * 32)
    envelope = _codex_envelope(store, organization, agent)

    class Transport:
        def __init__(self):
            self.calls: list[str] = []

        async def call(self, _org, _agent, method, _params):
            self.calls.append(method)
            if method == "thread/resume":
                raise RuntimeUnavailable("native transport unavailable")
            raise AssertionError(method)

        async def connection_id(self, _org, _agent):
            return "connection"

    transport = Transport()
    with pytest.raises(RuntimeUnavailable, match="native transport unavailable"):
        asyncio.run(
            _codex_runtime_for_context(transport).recover_workspace_context(
                organization, agent, "thr_context", "/workspace/default/threads/context", envelope
            )
        )
    assert transport.calls == ["thread/resume"]


def test_codex_creation_saves_identity_before_a_lost_context_acknowledgement(tmp_path: Path):
    settings = ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "state" / "host.sqlite3",
        state_directory=tmp_path / "state",
    )
    store = HostStore(settings)
    store.initialize()
    organization, agent = str(uuid4()), str(uuid4())
    store.bind_organization(organization, "x" * 32)
    _codex_envelope(store, organization, agent)
    directory = "/workspace/default/threads/context"

    class Runtime:
        def __init__(self):
            self.store = store

        async def running_port(self, _org, _agent):
            return 1

        async def docker(self, *_args):
            return b""

        def name(self, _agent):
            return "agent"

        def employee_workspace_root(self, _org, _agent):
            return tmp_path / "workspaces" / organization / agent

    class Transport:
        def __init__(self):
            self.calls: list[str] = []

        async def call(self, _org, _agent, method, _params):
            self.calls.append(method)
            if method == "thread/start":
                return _codex_receipt("thr_context", directory)
            if method == "thread/inject_items":
                raise RuntimeUnavailable("lost context acknowledgement")
            raise AssertionError(method)

        async def connection_id(self, _org, _agent):
            return "connection"

    runtime = _codex_runtime_for_context(Transport())
    runtime.runtime = cast(Any, Runtime())

    with pytest.raises(RuntimeUnavailable, match="lost context acknowledgement"):
        asyncio.run(
            runtime.create_session(organization, agent, "Requested", "default", directory=directory)
        )
    saved = store.session(organization, agent, "thr_context")
    assert saved["directory"] == directory
    assert saved["title"] == "Requested"
    assert saved["runtime_type"] == "codex"


def test_codex_recovery_reads_string_inventory_and_recovers_workspace_context(tmp_path: Path):
    settings = ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "state" / "host.sqlite3",
        state_directory=tmp_path / "state",
        workspace_root=tmp_path / "workspaces",
    )
    store = HostStore(settings)
    store.initialize()
    organization, agent, creation = (str(uuid4()), str(uuid4()), str(uuid4()))
    store.bind_organization(organization, "x" * 32)
    envelope = HostAgentConfiguration(
        host_id=store.instance_id,
        organization_id=organization,
        agent_id=agent,
        version=1,
        name="Engineer",
        configuration={"runtime_type": "codex"},
    )
    store.stage_agent(envelope)
    store.mark_applied(envelope)
    directory = str(
        (tmp_path / "workspaces" / organization / agent / "threads" / creation).resolve()
    )
    store.reserve_workspace_creation(organization, agent, creation, "a" * 64, directory, None, None)
    store.mark_workspace_prepared(organization, agent, creation, None)
    store.mark_workspace_native_attempted(organization, agent, creation)

    class Transport:
        def __init__(self):
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def call(self, _org, _agent, method, params):
            self.calls.append((method, dict(params)))
            if method == "thread/list":
                if params.get("cursor") is None:
                    return {
                        "data": [{"id": "thr_lost"}],
                        "nextCursor": "second",
                    }
                return {"data": [], "nextCursor": None}
            if method == "thread/loaded/list":
                return {
                    "data": ["thr_lost"],
                    "nextCursor": None,
                }
            if method == "thread/read":
                return {
                    "thread": {
                        "id": "thr_lost",
                        "sessionId": "thr_lost",
                        "cwd": directory,
                        "parentThreadId": None,
                        "forkedFromId": None,
                        "title": "Recovered",
                    }
                }
            raise AssertionError(method)

    transport = Transport()
    context_calls: list[tuple[str, str]] = []

    async def recover_workspace_context(_org, _agent, thread_id, recovered_directory, _envelope):
        context_calls.append((thread_id, recovered_directory))

    runtime = DockerRuntime.__new__(DockerRuntime)
    runtime.store = store
    runtime.workspace_root = settings.agent_workspace_root
    runtime.codex = cast(
        Any,
        SimpleNamespace(transport=transport, recover_workspace_context=recover_workspace_context),
    )

    receipt = asyncio.run(
        runtime._recover_codex_creation(organization, agent, creation, directory, "Requested")
    )

    assert receipt == {"id": "thr_lost", "title": "Recovered", "directory": directory}
    assert store.session(organization, agent, "thr_lost")["runtime_type"] == "codex"
    assert store.workspace_creation(organization, agent, creation)["state"] == "completed"
    assert context_calls == [("thr_lost", directory)]
    assert [method for method, _params in transport.calls] == [
        "thread/list",
        "thread/list",
        "thread/loaded/list",
        "thread/read",
    ]


def test_codex_recovery_rejects_a_forked_thread_even_at_the_exact_directory(tmp_path: Path):
    settings = ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "state" / "host.sqlite3",
        state_directory=tmp_path / "state",
        workspace_root=tmp_path / "workspaces",
    )
    store = HostStore(settings)
    store.initialize()
    organization, agent, creation = (str(uuid4()), str(uuid4()), str(uuid4()))
    store.bind_organization(organization, "x" * 32)
    envelope = HostAgentConfiguration(
        host_id=store.instance_id,
        organization_id=organization,
        agent_id=agent,
        version=1,
        name="Engineer",
        configuration={"runtime_type": "codex"},
    )
    store.stage_agent(envelope)
    store.mark_applied(envelope)
    directory = str(
        (tmp_path / "workspaces" / organization / agent / "threads" / creation).resolve()
    )
    store.reserve_workspace_creation(organization, agent, creation, "a" * 64, directory, None, None)
    store.mark_workspace_prepared(organization, agent, creation, None)
    store.mark_workspace_native_attempted(organization, agent, creation)

    class Transport:
        async def call(self, _org, _agent, method, _params):
            if method in {"thread/list", "thread/loaded/list"}:
                return {"data": ["thr_fork"], "nextCursor": None}
            if method == "thread/read":
                return {
                    "thread": {
                        "id": "thr_fork",
                        "sessionId": "thr_other",
                        "cwd": directory,
                        "parentThreadId": None,
                        "forkedFromId": "thr_source",
                    }
                }
            raise AssertionError(method)

    runtime = DockerRuntime.__new__(DockerRuntime)
    runtime.store = store
    runtime.workspace_root = settings.agent_workspace_root
    runtime.codex = cast(Any, SimpleNamespace(transport=Transport()))

    assert (
        asyncio.run(
            runtime._recover_codex_creation(organization, agent, creation, directory, "Requested")
        )
        is None
    )
    with pytest.raises(LookupError):
        store.session(organization, agent, "thr_fork")


def test_unturned_codex_recovery_keeps_its_reservation_uncertain(tmp_path: Path):
    settings = ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "state" / "host.sqlite3",
        state_directory=tmp_path / "state",
        workspace_root=tmp_path / "workspaces",
    )
    store = HostStore(settings)
    store.initialize()
    organization, agent, creation = (str(uuid4()), str(uuid4()), str(uuid4()))
    store.bind_organization(organization, "x" * 32)
    envelope = HostAgentConfiguration(
        host_id=store.instance_id,
        organization_id=organization,
        agent_id=agent,
        version=1,
        name="Engineer",
        configuration={"runtime_type": "codex"},
    )
    store.stage_agent(envelope)
    store.mark_applied(envelope)
    runtime = DockerRuntime.__new__(DockerRuntime)
    runtime.store = store
    runtime.workspace_root = settings.agent_workspace_root
    runtime.locks = {}
    repository_url, checkout_branch = "https://git.example.test/repository.git", "test"
    directory = str(
        workspace_paths(
            runtime.workspace_root, organization, agent, creation, repository_url
        ).directory
    )
    fingerprint = runtime._creation_fingerprint(
        "Lost receipt", "default", directory, repository_url, checkout_branch, None, None, None
    )
    store.reserve_workspace_creation(
        organization, agent, creation, fingerprint, directory, repository_url, checkout_branch
    )
    store.mark_workspace_prepared(organization, agent, creation, "revision")
    store.mark_workspace_native_attempted(organization, agent, creation)

    class Transport:
        def __init__(self):
            self.calls: list[str] = []

        async def call(self, _org, _agent, method, _params):
            self.calls.append(method)
            if method == "thread/list":
                return {"data": [{"id": "thr_lost"}], "nextCursor": None}
            if method == "thread/loaded/list":
                return {"data": ["thr_lost"], "nextCursor": None}
            if method == "thread/read":
                return {
                    "thread": {
                        "id": "thr_lost",
                        "sessionId": "thr_lost",
                        "cwd": directory,
                        "parentThreadId": None,
                        "forkedFromId": None,
                    }
                }
            raise AssertionError(method)

    transport = Transport()

    async def recover_workspace_context(*_args):
        raise RuntimeUnavailable("native recovery is unavailable")

    runtime.codex = cast(
        Any,
        SimpleNamespace(transport=transport, recover_workspace_context=recover_workspace_context),
    )

    with pytest.raises(WorkspaceCreationUncertain):
        asyncio.run(
            runtime.create_session(
                organization,
                agent,
                "Lost receipt",
                "default",
                creation_id=creation,
                repository_url=repository_url,
                checkout_branch=checkout_branch,
            )
        )

    reservation = store.workspace_creation(organization, agent, creation)
    assert reservation["state"] == "uncertain"
    assert reservation["native_receipt"] is None
    assert "thread/start" not in transport.calls


def test_opencode_recovery_requires_one_exact_root_directory(tmp_path: Path, monkeypatch):
    settings = ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "state" / "host.sqlite3",
        state_directory=tmp_path / "state",
        workspace_root=tmp_path / "workspaces",
    )
    store = HostStore(settings)
    store.initialize()
    organization, agent, creation = (str(uuid4()), str(uuid4()), str(uuid4()))
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
    directory = str(
        (tmp_path / "workspaces" / organization / agent / "threads" / creation).resolve()
    )
    store.reserve_workspace_creation(organization, agent, creation, "a" * 64, directory, None, None)
    store.mark_workspace_prepared(organization, agent, creation, None)
    store.mark_workspace_native_attempted(organization, agent, creation)

    async def request(_org, _agent, path, **_kwargs):
        assert path == "/session"
        return [
            {
                "id": "ses_wrong",
                "directory": directory + "-other",
                "parentID": None,
                "metadata": {"fesnyng_creation_id": creation},
            }
        ]

    runtime = DockerRuntime.__new__(DockerRuntime)
    runtime.store = store
    monkeypatch.setattr(runtime, "request", request)

    assert (
        asyncio.run(
            runtime._recover_opencode_creation(
                organization, agent, creation, directory, "Requested"
            )
        )
        is None
    )
    with pytest.raises(LookupError):
        store.session(organization, agent, "ses_wrong")


def test_legacy_workspace_context_does_not_require_the_new_employee_mount(
    tmp_path: Path, monkeypatch
):
    settings = ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "state" / "host.sqlite3",
        state_directory=tmp_path / "state",
        workspace_root=tmp_path / "unmounted-new-root",
    )
    runtime = DockerRuntime.__new__(DockerRuntime)
    runtime.workspace_root = settings.agent_workspace_root
    organization, agent = str(uuid4()), str(uuid4())

    async def inspect(_organization, _agent):
        return {"state": {"Running": True}}

    async def docker(*args, **_kwargs):
        assert args[-2] == "/workspace/default/legacy"
        return b"absent\0\0not_applicable\0\0not_applicable\0\0\0\0\0\0"

    monkeypatch.setattr(runtime, "inspect", inspect)
    monkeypatch.setattr(runtime, "docker", docker)
    monkeypatch.setattr(runtime, "name", lambda _agent: "legacy-agent")

    assert asyncio.run(
        runtime.workspace_context(organization, agent, "/workspace/default/legacy")
    ) == {
        "repository": {"state": "absent"},
        "branch": {"state": "not_applicable"},
        "changes": {"state": "not_applicable"},
    }


def test_standalone_git_workspace_fork_keeps_changes_without_copying_git_pointer(tmp_path: Path):
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.mkdir()
    for command in (
        ["git", "init", "-q", str(source)],
        ["git", "-C", str(source), "config", "user.email", "test@example.invalid"],
        ["git", "-C", str(source), "config", "user.name", "Test"],
    ):
        subprocess.run(command, check=True)
    source.joinpath("tracked.txt").write_text("tracked")
    subprocess.run(["git", "-C", str(source), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "initial"], check=True)
    source.joinpath("tracked.txt").unlink()
    source.joinpath("untracked.txt").write_text("retained")

    subprocess.run(
        ["sh", "-c", STANDALONE_GIT_FORK_SCRIPT, "fork", str(source), str(destination), "forked"],
        check=True,
    )

    assert destination.joinpath(".git").is_dir()
    assert not destination.joinpath("tracked.txt").exists()
    assert destination.joinpath("untracked.txt").read_text() == "retained"


def test_prepared_git_workspace_is_reconciled_after_an_interrupted_reservation_update(
    tmp_path: Path,
):
    source, remote = tmp_path / "source", tmp_path / "remote.git"
    source.mkdir()
    for command in (
        ["git", "init", "-q", str(source)],
        ["git", "-C", str(source), "config", "user.email", "test@example.invalid"],
        ["git", "-C", str(source), "config", "user.name", "Test"],
    ):
        subprocess.run(command, check=True)
    source.joinpath("tracked.txt").write_text("tracked")
    subprocess.run(["git", "-C", str(source), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "initial"], check=True)
    branch = subprocess.run(
        ["git", "-C", str(source), "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(["git", "clone", "--bare", "-q", str(source), str(remote)], check=True)

    employee = tmp_path / "employee"
    employee.mkdir()
    bare, directory, creation = (
        employee / "repositories" / "source.git",
        employee / "threads" / str(uuid4()),
        str(uuid4()),
    )
    prepared = (
        subprocess.run(
            [
                "sh",
                "-c",
                GIT_PREPARE_SCRIPT,
                "prepare",
                str(employee),
                str(bare),
                str(directory),
                str(remote),
                branch,
                creation,
            ],
            check=True,
            capture_output=True,
        )
        .stdout.decode()
        .split("\0")
    )
    assert prepared[0] == "prepared"

    reconciled = (
        subprocess.run(
            [
                "sh",
                "-c",
                GIT_PREPARED_RECONCILE_SCRIPT,
                "reconcile",
                str(employee),
                str(bare),
                str(directory),
                str(remote),
                branch,
                creation,
                "",
            ],
            check=True,
            capture_output=True,
        )
        .stdout.decode()
        .split("\0")
    )
    assert reconciled[0] == "prepared"
    assert reconciled[1] == prepared[1]

    subprocess.run(["git", "-C", str(source), "remote", "add", "origin", str(remote)], check=True)
    source.joinpath("advanced.txt").write_text("new remote tip")
    subprocess.run(["git", "-C", str(source), "add", "advanced.txt"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "advance remote"], check=True)
    subprocess.run(["git", "-C", str(source), "push", "-q", "origin", branch], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(bare),
            "fetch",
            "-q",
            "origin",
            f"+refs/heads/{branch}:refs/remotes/fesnyng/{branch}",
        ],
        check=True,
    )

    subprocess.run(["git", "-C", str(directory), "switch", "--detach", "-q"], check=True)
    subprocess.run(["git", "-C", str(directory), "branch", "-D", f"fesnyng/{creation}"], check=True)
    interrupted = (
        subprocess.run(
            [
                "sh",
                "-c",
                GIT_PREPARED_RECONCILE_SCRIPT,
                "reconcile",
                str(employee),
                str(bare),
                str(directory),
                str(remote),
                branch,
                creation,
                prepared[1],
            ],
            check=True,
            capture_output=True,
        )
        .stdout.decode()
        .split("\0")
    )
    assert interrupted[:2] == ["prepared", prepared[1]]
    assert (
        subprocess.run(
            ["git", "-C", str(directory), "branch", "--show-current"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == f"fesnyng/{creation}"
    )

    rejected = subprocess.run(
        [
            "sh",
            "-c",
            GIT_PREPARED_RECONCILE_SCRIPT,
            "reconcile",
            str(employee),
            str(bare),
            str(directory),
            "https://example.invalid/changed.git",
            branch,
            creation,
            prepared[1],
        ],
        check=True,
        capture_output=True,
    ).stdout.decode()
    assert rejected == "invalid\0"
