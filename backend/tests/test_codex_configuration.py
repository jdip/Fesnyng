import asyncio
import secrets
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from fesnyng_backend.agent_models import AgentConfiguration, OrganizationPolicy
from fesnyng_backend.codex_runtime import CodexRuntime, native_policy
from fesnyng_backend.host_configuration import HostConfiguration
from fesnyng_backend.host_credentials import CredentialStore
from fesnyng_backend.host_dispatch import DispatchStore
from fesnyng_backend.host_interactions import Interactions
from fesnyng_backend.host_models import HostAgentConfiguration
from fesnyng_backend.host_runtime import RuntimeUnavailable, _assert_codex_threads_quiet
from fesnyng_backend.host_store import HostStore
from fesnyng_backend.settings import ServiceSettings


class Native:
    def lock(self, agent_id: str) -> asyncio.Lock:
        return asyncio.Lock()

    async def assert_quiet(self, organization_id: str, agent_id: str) -> None:
        return None

    async def configure(self, envelope: HostAgentConfiguration) -> None:
        return None

    async def request(
        self, organization_id, agent_id, path, *, method="GET", body=None, directory=None
    ):
        raise AssertionError("OpenCode request on Codex runtime")


def test_codex_policy_uses_only_equivalent_native_controls():
    base = HostAgentConfiguration(
        host_id=uuid4(),
        organization_id=uuid4(),
        agent_id=uuid4(),
        version=1,
        name="Agent",
        configuration=AgentConfiguration(runtime_type="codex"),
    )
    assert native_policy(base, []) == {
        "approvalPolicy": "never",
        "approvalsReviewer": "user",
        "sandbox": "danger-full-access",
    }
    ask = base.model_copy(update={"policy": OrganizationPolicy(default_permission="ask")})
    with pytest.raises(RuntimeUnavailable, match="default ask"):
        native_policy(ask, [])
    with pytest.raises(RuntimeUnavailable, match="default deny"):
        native_policy(
            base.model_copy(update={"policy": OrganizationPolicy(default_permission="deny")}), []
        )
    with pytest.raises(RuntimeUnavailable, match="mandatory"):
        native_policy(
            base.model_copy(
                update={
                    "policy": OrganizationPolicy(
                        mandatory_permissions=[
                            {"permission": "shell", "pattern": "*", "action": "ask"}
                        ]
                    )
                }
            ),
            [],
        )
    with pytest.raises(RuntimeUnavailable, match="thread overrides"):
        native_policy(
            base,
            [{"permission": "shell", "pattern": "*", "action": "ask"}],
        )


def test_reconnect_resume_reapplies_and_verifies_the_applied_policy_before_caching():
    envelope = HostAgentConfiguration(
        host_id=uuid4(),
        organization_id=uuid4(),
        agent_id=uuid4(),
        version=1,
        name="Agent",
        configuration=AgentConfiguration(runtime_type="codex"),
    )

    class Store:
        def agent(self, _org, _agent):
            return {"applied_envelope": envelope.model_dump_json()}

    class Runtime:
        store = Store()

        async def running_port(self, _org, _agent):
            return 1

    class Transport:
        def __init__(self, sandbox="dangerFullAccess"):
            self.sandbox, self.calls = sandbox, []

        async def connection_id(self, _org, _agent):
            return "reconnected"

        async def call(self, org, agent, method, params):
            self.calls.append((org, agent, method, params))
            return {
                "thread": {"id": "thr_1"},
                "approvalPolicy": "never",
                "approvalsReviewer": "user",
                "sandbox": {"type": self.sandbox},
            }

    runtime = CodexRuntime.__new__(CodexRuntime)
    runtime.runtime = cast(Any, Runtime())
    runtime.resumed_connections = {}
    runtime.started_policies = {}
    transport = Transport()
    runtime.transport = cast(Any, transport)
    asyncio.run(runtime._resume(str(envelope.organization_id), str(envelope.agent_id), "thr_1"))
    assert transport.calls[0][2:] == (
        "thread/resume",
        {
            "threadId": "thr_1",
            "approvalPolicy": "never",
            "approvalsReviewer": "user",
            "sandbox": "danger-full-access",
        },
    )
    assert runtime.resumed_connections

    runtime.resumed_connections = {}
    runtime.transport = cast(Any, Transport(sandbox="workspaceWrite"))
    with pytest.raises(RuntimeUnavailable, match="required native policy"):
        asyncio.run(runtime._resume(str(envelope.organization_id), str(envelope.agent_id), "thr_1"))
    assert runtime.resumed_connections == {}


def test_codex_quiet_state_includes_native_subagent_descendants():
    root = {"id": "root", "parentThreadId": None, "status": {"type": "idle"}}
    child = {"id": "child", "parentThreadId": "root", "status": {"type": "active"}}
    with pytest.raises(RuntimeUnavailable, match="native work is active"):
        _assert_codex_threads_quiet({"root"}, [root, child])
    _assert_codex_threads_quiet(
        {"root"},
        [root, {"id": "child", "parentThreadId": "root", "status": {"type": "idle"}}],
    )
    with pytest.raises(RuntimeUnavailable, match="missing"):
        _assert_codex_threads_quiet({"root"}, [])


def test_existing_agent_harness_change_stays_pending_even_without_threads(tmp_path: Path):
    host = HostStore(
        ServiceSettings(
            service="agent-host", database_path=tmp_path / "host.sqlite3", state_directory=tmp_path
        )
    )
    host.initialize()
    organization_id, agent_id = str(uuid4()), str(uuid4())
    host.bind_organization(organization_id, secrets.token_urlsafe(32))
    initial = HostAgentConfiguration(
        host_id=host.instance_id,
        organization_id=organization_id,
        agent_id=agent_id,
        version=1,
        name="Agent",
    )
    runtime = Native()
    configuration = HostConfiguration(
        host,
        runtime,
        CredentialStore(tmp_path / "credentials.sqlite3"),
        Interactions(host, runtime),
        DispatchStore(host),
    )
    configuration.credentials.initialize()
    configuration.dispatch_store.initialize()
    asyncio.run(configuration.apply(initial))

    codex = initial.model_copy(
        update={"version": 2, "configuration": AgentConfiguration(runtime_type="codex")}
    )
    with pytest.raises(RuntimeUnavailable, match="thread freeze workflow"):
        asyncio.run(configuration.apply(codex))

    status = host.agent_status(organization_id, agent_id)
    assert status["applied_version"] == 1
    assert status["desired_version"] == 2
    assert status["runtime_state"] == "pending"
    assert status["error"] == "Harness changes require the thread freeze workflow"


def test_codex_policy_update_completes_with_the_shared_runtime_lock(tmp_path: Path):
    """Configuration owns the lock while applying each Codex thread policy."""
    host = HostStore(
        ServiceSettings(
            service="agent-host", database_path=tmp_path / "host.sqlite3", state_directory=tmp_path
        )
    )
    host.initialize()
    organization_id, agent_id = str(uuid4()), str(uuid4())
    host.bind_organization(organization_id, secrets.token_urlsafe(32))

    class Codex:
        def __init__(self):
            self.calls: list[str] = []

        async def apply_policy(self, _org, _agent, thread_id, _envelope, _overrides):
            self.calls.append(thread_id)

    class SharedLockNative(Native):
        def __init__(self):
            self._lock = asyncio.Lock()
            self.codex = Codex()

        def lock(self, agent_id: str) -> asyncio.Lock:
            return self._lock

    runtime = SharedLockNative()
    credentials = CredentialStore(tmp_path / "credentials.sqlite3")
    credentials.initialize()
    dispatches = DispatchStore(host)
    dispatches.initialize()
    interactions = Interactions(host, runtime)
    interactions.initialize()
    configuration = HostConfiguration(host, runtime, credentials, interactions, dispatches)
    initial = HostAgentConfiguration(
        host_id=host.instance_id,
        organization_id=organization_id,
        agent_id=agent_id,
        version=1,
        name="Codex agent",
        configuration=AgentConfiguration(runtime_type="codex"),
    )
    asyncio.run(configuration.apply(initial))
    host.save_session(
        organization_id, agent_id, "thr_codex", "/workspace/codex", "Codex", runtime_type="codex"
    )
    updated = initial.model_copy(update={"version": 2, "policy_version": 2})

    async def apply_with_timeout():
        return await asyncio.wait_for(configuration.apply(updated), timeout=0.2)

    status = asyncio.run(apply_with_timeout())
    assert status["applied_version"] == 2
    assert runtime.codex.calls == ["thr_codex"]
