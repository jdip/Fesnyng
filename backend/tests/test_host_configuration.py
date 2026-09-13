import asyncio
import secrets
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from fesnyng_backend.agent_host import create_app as create_host_app
from fesnyng_backend.agent_models import AgentConfiguration, OrganizationPolicy, PermissionRule
from fesnyng_backend.host_configuration import HostConfiguration
from fesnyng_backend.host_credentials import CredentialStore
from fesnyng_backend.host_dispatch import DispatchStore, Submission
from fesnyng_backend.host_interactions import Interactions
from fesnyng_backend.host_models import Actor, HostAgentConfiguration
from fesnyng_backend.host_runtime import RuntimeUnavailable
from fesnyng_backend.host_store import HostStore
from fesnyng_backend.settings import ServiceSettings


def test_apply_stages_desired_configuration_then_configures_after_quiet(tmp_path):
    host, configuration, runtime, organization_id, agent_id = _configuration(tmp_path)
    envelope = HostAgentConfiguration(
        host_id=host.instance_id,
        organization_id=organization_id,
        agent_id=agent_id,
        version=1,
        name="Reconciled agent",
    )

    status = asyncio.run(configuration.apply(envelope))

    assert status["desired_version"] == status["applied_version"] == 1
    assert runtime.events == ["quiet", "configure"]


def test_policy_suffix_is_applied_before_global_configuration(tmp_path):
    host, configuration, runtime, organization_id, agent_id = _configuration(tmp_path)
    baseline = HostAgentConfiguration(
        host_id=host.instance_id,
        organization_id=organization_id,
        agent_id=agent_id,
        version=1,
        name="Reconciled agent",
    )
    asyncio.run(configuration.apply(baseline))
    session_id = "ses_policy"
    host.save_session(organization_id, agent_id, session_id, "/workspace/policy", "Policy")
    runtime.session_id = session_id
    runtime.events.clear()
    candidate = baseline.model_copy(
        update={
            "version": 2,
            "policy_version": 2,
            "policy": OrganizationPolicy(
                default_permission="ask",
                mandatory_permissions=[PermissionRule(permission="shell", action="deny")],
            ),
        }
    )

    status = asyncio.run(configuration.apply(candidate))

    assert status["applied_version"] == 2
    assert runtime.events.index("dispose") < runtime.events.index("policy_patch")
    assert runtime.events.index("policy_patch") < runtime.events.index("quiet")
    assert runtime.events.index("quiet") < runtime.events.index("configure")
    assert runtime.permissions[-3:] == [
        {"permission": "*", "pattern": "*", "action": "ask"},
        {"permission": "shell", "pattern": "*", "action": "deny"},
        {"permission": "task", "pattern": "*", "action": "deny"},
    ]


def test_unresolved_delivery_blocks_global_config_but_reconcile_retries_after_resolution(tmp_path):
    host, configuration, runtime, organization_id, agent_id = _configuration(tmp_path)
    baseline = HostAgentConfiguration(
        host_id=host.instance_id,
        organization_id=organization_id,
        agent_id=agent_id,
        version=1,
        name="Reconciled agent",
    )
    asyncio.run(configuration.apply(baseline))
    session_id = "ses_pending"
    host.save_session(organization_id, agent_id, session_id, "/workspace/pending", "Pending")
    runtime.session_id = session_id
    pending = configuration.dispatch_store.enqueue(
        organization_id,
        agent_id,
        session_id,
        Submission(id=uuid4(), text="Uncertain native effect"),
        Actor(kind="human", id=uuid4(), name="Owner"),
    )
    configuration.dispatch_store.change(pending, "uncertain", error="Needs reconciliation")
    candidate = baseline.model_copy(
        update={
            "version": 2,
            "policy_version": 2,
            "policy": OrganizationPolicy(
                mandatory_permissions=[PermissionRule(permission="shell", action="deny")]
            ),
        }
    )
    runtime.events.clear()

    with pytest.raises(RuntimeUnavailable, match="delivery effects"):
        asyncio.run(configuration.apply(candidate))
    assert "policy_patch" in runtime.events
    assert "configure" not in runtime.events
    state = host.agent_status(organization_id, agent_id)
    assert state["desired_version"] == 2 and state["applied_version"] == 1
    assert state["runtime_state"] == "pending"

    uncertain = configuration.dispatch_store.get(organization_id, agent_id, pending["id"])
    configuration.dispatch_store.change(uncertain, "completed")
    retried = asyncio.run(configuration.reconcile_once())
    assert retried == {agent_id: "applied"}
    assert host.agent_status(organization_id, agent_id)["applied_version"] == 2


def test_queued_delivery_does_not_block_instruction_reconfiguration(tmp_path):
    host, configuration, _, organization_id, agent_id = _configuration(tmp_path)
    baseline = HostAgentConfiguration(
        host_id=host.instance_id,
        organization_id=organization_id,
        agent_id=agent_id,
        version=1,
        name="Reconciled agent",
    )
    asyncio.run(configuration.apply(baseline))
    host.save_session(organization_id, agent_id, "ses_queued", "/workspace/queued", "Queued")
    configuration.dispatch_store.enqueue(
        organization_id,
        agent_id,
        "ses_queued",
        Submission(id=uuid4(), text="Queued but not submitted"),
        Actor(kind="human", id=uuid4(), name="Owner"),
    )
    candidate = baseline.model_copy(
        update={
            "version": 2,
            "configuration": AgentConfiguration(instructions="Updated instructions"),
        }
    )

    status = asyncio.run(configuration.apply(candidate))

    assert status["desired_version"] == status["applied_version"] == 2


def test_stopped_agent_retains_pending_configuration_until_started(tmp_path):
    host, configuration, runtime, organization_id, agent_id = _configuration(tmp_path)
    baseline = HostAgentConfiguration(
        host_id=host.instance_id,
        organization_id=organization_id,
        agent_id=agent_id,
        version=1,
        name="Reconciled agent",
    )
    asyncio.run(configuration.apply(baseline))
    host.set_lifecycle_state(organization_id, agent_id, desired="stopped", state="stopped")
    runtime.events.clear()
    updated = baseline.model_copy(
        update={
            "version": 2,
            "configuration": AgentConfiguration(instructions="Apply after Start"),
        }
    )

    status = asyncio.run(configuration.apply(updated))

    assert status["desired_version"] == 2 and status["applied_version"] == 1
    assert status["desired_state"] == status["lifecycle_state"] == "stopped"
    assert runtime.events == []
    assert asyncio.run(configuration.reconcile_once()) == {}


def test_pending_lifecycle_retries_same_version_configuration_after_start(tmp_path):
    host, configuration, runtime, organization_id, agent_id = _configuration(tmp_path)
    envelope = HostAgentConfiguration(
        host_id=host.instance_id,
        organization_id=organization_id,
        agent_id=agent_id,
        version=1,
        name="Reconciled agent",
    )
    asyncio.run(configuration.apply(envelope))
    host.set_lifecycle_state(organization_id, agent_id, desired="running", state="pending")
    runtime.events.clear()

    result = asyncio.run(configuration.reconcile_once())

    assert result == {agent_id: "applied"}
    assert "configure" in runtime.events
    status = host.agent_status(organization_id, agent_id)
    assert status["desired_version"] == status["applied_version"] == 1
    assert status["lifecycle_state"] == "running"


def test_apply_waiting_for_runtime_lock_cannot_restart_an_agent_stopped_in_the_race(tmp_path):
    host, configuration, runtime, organization_id, agent_id = _configuration(tmp_path)
    baseline = HostAgentConfiguration(
        host_id=host.instance_id,
        organization_id=organization_id,
        agent_id=agent_id,
        version=1,
        name="Reconciled agent",
    )
    asyncio.run(configuration.apply(baseline))
    updated = baseline.model_copy(update={"version": 2})
    runtime.events.clear()

    async def race():
        lock = runtime.lock(agent_id)
        await lock.acquire()
        applying = asyncio.create_task(configuration.apply(updated))
        for _ in range(100):
            if host.agent_status(organization_id, agent_id)["desired_version"] == 2:
                break
            await asyncio.sleep(0)
        else:
            raise AssertionError("Configuration was not staged")
        host.set_lifecycle_state(organization_id, agent_id, desired="stopped", state="stopped")
        lock.release()
        with pytest.raises(RuntimeUnavailable, match="lifecycle transition"):
            await applying

    asyncio.run(race())

    assert "configure" not in runtime.events
    status = host.agent_status(organization_id, agent_id)
    assert status["desired_state"] == status["lifecycle_state"] == "stopped"
    assert status["desired_version"] == 2 and status["applied_version"] == 1


def test_apply_rechecks_desired_envelope_after_thread_policy_work(tmp_path):
    host, configuration, runtime, organization_id, agent_id = _configuration(tmp_path)
    baseline = HostAgentConfiguration(
        host_id=host.instance_id,
        organization_id=organization_id,
        agent_id=agent_id,
        version=1,
        name="Reconciled agent",
    )
    asyncio.run(configuration.apply(baseline))
    session_id = "ses_race"
    host.save_session(organization_id, agent_id, session_id, "/workspace/race", "Race")
    runtime.session_id = session_id
    candidate = baseline.model_copy(
        update={
            "version": 2,
            "policy_version": 2,
            "policy": OrganizationPolicy(
                mandatory_permissions=[PermissionRule(permission="shell", action="deny")]
            ),
        }
    )
    replacement = candidate.model_copy(update={"version": 3, "policy_version": 3})
    runtime.on_policy_patch = lambda: host.stage_agent(replacement)
    runtime.events.clear()

    with pytest.raises(RuntimeUnavailable, match="changed during application"):
        asyncio.run(configuration.apply(candidate))
    assert "policy_patch" in runtime.events
    assert "configure" not in runtime.events
    state = host.agent_status(organization_id, agent_id)
    assert state["desired_version"] == 3 and state["applied_version"] == 1


def test_reconcile_does_not_delay_another_agent_while_one_configuration_blocks(tmp_path):
    host, configuration, runtime, organization_id, first_agent = _configuration(tmp_path)
    second_agent = str(uuid4())
    first = HostAgentConfiguration(
        host_id=host.instance_id,
        organization_id=organization_id,
        agent_id=first_agent,
        version=1,
        name="Blocked agent",
    )
    second = HostAgentConfiguration(
        host_id=host.instance_id,
        organization_id=organization_id,
        agent_id=second_agent,
        version=1,
        name="Independent agent",
    )
    host.stage_agent(first)
    host.stage_agent(second)
    runtime.blocked_agent = first_agent

    async def reconcile():
        task = asyncio.create_task(configuration.reconcile_once())
        await runtime.first_configure_started.wait()
        await asyncio.wait_for(runtime.second_configured.wait(), timeout=0.2)
        assert not task.done()
        runtime.release_first_configure.set()
        return await task

    result = asyncio.run(reconcile())

    assert result == {first_agent: "applied", second_agent: "applied"}
    assert host.agent_status(organization_id, second_agent)["applied_version"] == 1


def test_host_routes_delegate_apply_and_block_replacement_for_unsettled_dispatch(tmp_path):
    settings = ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "host.sqlite3",
        state_directory=tmp_path / "state",
    )
    app = create_host_app(settings)
    host = app.state.host_store
    organization_id, agent_id = str(uuid4()), str(uuid4())
    binding = secrets.token_urlsafe(32)
    host.bind_organization(organization_id, binding)
    runtime = Native()
    interactions = Interactions(host, runtime)
    interactions.initialize()
    app.state.host_runtime = runtime
    app.state.host_configuration = HostConfiguration(
        host, runtime, app.state.credential_store, interactions, app.state.dispatch_store
    )
    envelope = HostAgentConfiguration(
        host_id=host.instance_id,
        organization_id=organization_id,
        agent_id=agent_id,
        version=1,
        name="Route agent",
    )

    async def request():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://host",
            headers={"Authorization": f"Bearer {binding}"},
        ) as client:
            base = f"/organizations/{organization_id}/agents/{agent_id}"
            applied = await client.put(base, json=envelope.model_dump(mode="json"))
            assert applied.status_code == 200
            host.save_session(
                organization_id, agent_id, "ses_replace", "/workspace/replace", "Replace"
            )
            receipt = app.state.dispatch_store.enqueue(
                organization_id,
                agent_id,
                "ses_replace",
                Submission(id=uuid4(), text="Unknown native effect"),
                Actor(kind="human", id=uuid4(), name="Owner"),
            )
            app.state.dispatch_store.change(receipt, "uncertain", error="Investigate")
            blocked = await client.post(f"{base}/replace")
            assert blocked.status_code == 503
            assert blocked.json()["detail"] == "Agent replacement needs delivery reconciliation"

    asyncio.run(request())
    assert "replace" not in runtime.events


def _configuration(tmp_path: Path):
    settings = ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "host.sqlite3",
        state_directory=tmp_path / "state",
    )
    host = HostStore(settings)
    host.initialize()
    organization_id, agent_id = str(uuid4()), str(uuid4())
    host.bind_organization(organization_id, secrets.token_urlsafe(32))
    runtime = Native()
    credentials = CredentialStore(tmp_path / "credentials.sqlite3")
    credentials.initialize()
    interactions = Interactions(host, runtime)
    interactions.initialize()
    dispatches = DispatchStore(host)
    dispatches.initialize()
    return (
        host,
        HostConfiguration(host, runtime, credentials, interactions, dispatches),
        runtime,
        organization_id,
        agent_id,
    )


class Native:
    def __init__(self):
        self.events = []
        self.locks = {}
        self.permissions = []
        self.session_id = ""
        self.on_policy_patch = None
        self.blocked_agent = ""
        self.first_configure_started = asyncio.Event()
        self.release_first_configure = asyncio.Event()
        self.second_configured = asyncio.Event()

    def lock(self, agent_id):
        return self.locks.setdefault(agent_id, asyncio.Lock())

    async def assert_quiet(self, organization_id, agent_id):
        self.events.append("quiet")

    async def configure(self, envelope):
        self.events.append("configure")
        if str(envelope.agent_id) == self.blocked_agent:
            self.first_configure_started.set()
            await self.release_first_configure.wait()
        elif self.blocked_agent:
            self.second_configured.set()

    async def replace(self, organization_id, agent_id):
        self.events.append("replace")

    async def request(
        self, organization_id, agent_id, path, *, method="GET", body=None, directory=None
    ):
        if path.endswith("/children"):
            return []
        if path == "/session/status":
            return {self.session_id: {"type": "idle"}}
        if path == "/instance/dispose":
            self.events.append("dispose")
            return None
        if path == f"/session/{self.session_id}" and method == "PATCH":
            self.events.append("policy_patch")
            if not isinstance(body, dict):
                raise AssertionError("Missing policy body")
            self.permissions.extend(body["permission"])
            if self.on_policy_patch is not None:
                self.on_policy_patch()
            return None
        if path == f"/session/{self.session_id}":
            return {"permission": self.permissions}
        raise AssertionError(path)
