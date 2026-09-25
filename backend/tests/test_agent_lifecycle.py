import asyncio
import secrets
from uuid import uuid4

import pytest

from fesnyng_backend.agent_lifecycle import AgentLifecycle, HostLifecycleRequest
from fesnyng_backend.host_models import Actor, HostAgentConfiguration
from fesnyng_backend.host_runtime import DockerRuntime, RuntimeUnavailable
from fesnyng_backend.host_store import HostStore
from fesnyng_backend.settings import ServiceSettings


def test_stop_requires_confirmation_and_activity_code_bound_to_actor_and_snapshot(tmp_path):
    store, organization, agent = _applied_agent(tmp_path)
    runtime = Runtime()
    dispatcher = Dispatcher(activity="/workspace/default:ses_work:busy")
    lifecycle = AgentLifecycle(store, runtime, dispatcher, Configuration())
    owner = Actor(kind="human", id=uuid4(), name="Owner")

    ordinary = asyncio.run(
        lifecycle.perform(organization, agent, HostLifecycleRequest(action="stop", author=owner))
    )
    assert ordinary["confirmation_required"] is True
    assert "confirmation_code" not in ordinary

    active = asyncio.run(
        lifecycle.perform(
            organization,
            agent,
            HostLifecycleRequest(action="stop", confirmed=True, author=owner),
        )
    )
    code = active["confirmation_code"]
    assert active["confirmation_required"] is True and len(code) == 6

    with pytest.raises(ValueError, match="invalid or expired"):
        asyncio.run(
            lifecycle.perform(
                organization,
                agent,
                HostLifecycleRequest(
                    action="stop",
                    confirmed=True,
                    code=code,
                    author=Actor(kind="human", id=uuid4(), name="Other manager"),
                ),
            )
        )

    completed = asyncio.run(
        lifecycle.perform(
            organization,
            agent,
            HostLifecycleRequest(action="stop", confirmed=True, code=code, author=owner),
        )
    )
    assert completed["desired_state"] == completed["lifecycle_state"] == "stopped"
    assert completed["container_state"] == "exited"
    assert dispatcher.quiesced == 1
    restored = HostStore(store.settings).agent_status(organization, agent)
    assert restored["desired_state"] == restored["lifecycle_state"] == "stopped"

    with pytest.raises(ValueError, match="invalid or expired"):
        asyncio.run(
            lifecycle.perform(
                organization,
                agent,
                HostLifecycleRequest(action="stop", confirmed=True, code=code, author=owner),
            )
        )


def test_idle_to_active_race_preserves_previous_desired_state_and_requires_code(tmp_path):
    store, organization, agent = _applied_agent(tmp_path)
    runtime = Runtime()
    dispatcher = Dispatcher(activity="", raced_activity="/workspace/default:ses_new:busy")
    lifecycle = AgentLifecycle(store, runtime, dispatcher, Configuration())

    result = asyncio.run(
        lifecycle.perform(
            organization,
            agent,
            HostLifecycleRequest(
                action="stop",
                confirmed=True,
                author=Actor(kind="human", id=uuid4(), name="Owner"),
            ),
        )
    )

    assert result["confirmation_required"] is True
    assert result["desired_state"] == "running"
    assert result["lifecycle_state"] == "running"
    assert runtime.events == []


def test_missing_rebuild_preserves_identity_and_gates_unresolved_effects(tmp_path):
    store, organization, agent = _applied_agent(tmp_path)
    runtime = Runtime(container=None)
    dispatcher = Dispatcher(settled=False)
    lifecycle = AgentLifecycle(store, runtime, dispatcher, Configuration())
    owner = Actor(kind="human", id=uuid4(), name="Owner")

    result = asyncio.run(
        lifecycle.perform(
            organization,
            agent,
            HostLifecycleRequest(action="rebuild", confirmed=True, author=owner),
        )
    )

    assert runtime.events == ["rebuild"]
    assert dispatcher.reconciled == 1
    assert result["agent_id"] == agent
    assert result["desired_state"] == "running"
    assert result["lifecycle_state"] == "recovery_required"
    assert result["retry_action"] == "start"

    with pytest.raises(RuntimeUnavailable, match="delivery reconciliation"):
        asyncio.run(
            lifecycle.perform(
                organization,
                agent,
                HostLifecycleRequest(action="start", confirmed=True, author=owner),
            )
        )
    assert asyncio.run(lifecycle.status(organization, agent))["lifecycle_state"] == (
        "recovery_required"
    )

    dispatcher.settled = True
    recovered = asyncio.run(
        lifecycle.perform(
            organization,
            agent,
            HostLifecycleRequest(action="start", confirmed=True, author=owner),
        )
    )
    assert recovered["lifecycle_state"] == "running"


def test_interrupted_transition_recovers_fail_closed_after_host_restart(tmp_path):
    store, organization, agent = _applied_agent(tmp_path)
    store.set_lifecycle_state(organization, agent, state="transitioning")

    lifecycle = AgentLifecycle(store, Runtime(), Dispatcher(), Configuration())
    lifecycle.recover_interrupted()

    status = asyncio.run(lifecycle.status(organization, agent))
    assert status["lifecycle_state"] == "recovery_required"
    assert status["retry_action"] == "start"


def test_rebuild_validates_retained_volumes_before_removing_a_container(tmp_path):
    store, organization, agent = _applied_agent(tmp_path)

    class MissingVolumeRuntime(DockerRuntime):
        def __init__(self):
            super().__init__(store, "http://127.0.0.1:1", image="task-owned-image")
            self.commands = []

        async def docker(self, *args, content=None):
            self.commands.append(args)
            return b""

    runtime = MissingVolumeRuntime()

    with pytest.raises(RuntimeUnavailable, match="volume is missing"):
        asyncio.run(runtime.rebuild(organization, agent))

    assert runtime.commands[0][:2] == ("volume", "ls")
    assert not any(command[0] in {"rm", "run", "stop"} for command in runtime.commands)


@pytest.mark.parametrize(
    ("desired", "state"),
    [("stopped", "stopped"), ("running", "transitioning")],
)
def test_legacy_replace_rejects_lifecycle_state_before_container_mutation(tmp_path, desired, state):
    store, organization, agent = _applied_agent(tmp_path)
    store.set_lifecycle_state(organization, agent, desired=desired, state=state)

    class RecordingRuntime(DockerRuntime):
        def __init__(self):
            super().__init__(store, "http://127.0.0.1:1")
            self.commands = []

        async def docker(self, *args, content=None):
            self.commands.append(args)
            return b""

    runtime = RecordingRuntime()

    with pytest.raises(RuntimeUnavailable, match="lifecycle transition or stop"):
        asyncio.run(runtime.replace(organization, agent))

    assert runtime.commands == []


def test_same_agent_lifecycle_operations_are_serialized(tmp_path):
    store, organization, agent = _applied_agent(tmp_path)

    class BlockingRuntime(Runtime):
        def __init__(self):
            super().__init__()
            self.stop_entered = asyncio.Event()
            self.release_stop = asyncio.Event()

        async def stop(self, organization_id, agent_id):
            self.events.append("stop entered")
            self.stop_entered.set()
            await self.release_stop.wait()
            self.container = "exited"
            self.events.append("stop completed")

    runtime = BlockingRuntime()
    lifecycle = AgentLifecycle(store, runtime, Dispatcher(), Configuration())
    owner = Actor(kind="human", id=uuid4(), name="Owner")

    async def exercise():
        stop = asyncio.create_task(
            lifecycle.perform(
                organization,
                agent,
                HostLifecycleRequest(action="stop", confirmed=True, author=owner),
            )
        )
        await runtime.stop_entered.wait()
        restart = asyncio.create_task(
            lifecycle.perform(
                organization,
                agent,
                HostLifecycleRequest(action="restart", confirmed=True, author=owner),
            )
        )
        await asyncio.sleep(0)
        assert not restart.done()
        assert runtime.events == ["stop entered"]
        runtime.release_stop.set()
        await asyncio.gather(stop, restart)

    asyncio.run(exercise())

    assert runtime.events == ["stop entered", "stop completed", "restart"]


def test_unexpected_lifecycle_failure_does_not_leave_transition_gate_stranded(tmp_path):
    store, organization, agent = _applied_agent(tmp_path)

    class FailingRuntime(Runtime):
        async def restart(self, organization_id, agent_id):
            raise ValueError("unexpected runtime failure")

    lifecycle = AgentLifecycle(store, FailingRuntime(), Dispatcher(), Configuration())

    with pytest.raises(ValueError, match="unexpected runtime failure"):
        asyncio.run(
            lifecycle.perform(
                organization,
                agent,
                HostLifecycleRequest(
                    action="restart",
                    confirmed=True,
                    author=Actor(kind="human", id=uuid4(), name="Owner"),
                ),
            )
        )

    status = store.agent_status(organization, agent)
    assert status["lifecycle_state"] == "failed"
    assert status["error"] == "Lifecycle operation failed; inspect host logs"


def _applied_agent(tmp_path):
    store = HostStore(
        ServiceSettings(
            service="agent-host",
            database_path=tmp_path / "host.sqlite3",
            state_directory=tmp_path / "state",
        )
    )
    store.initialize()
    organization, agent = str(uuid4()), str(uuid4())
    store.bind_organization(organization, secrets.token_urlsafe(32))
    envelope = HostAgentConfiguration(
        host_id=store.instance_id,
        organization_id=organization,
        agent_id=agent,
        version=1,
        name="Lifecycle agent",
    )
    store.stage_agent(envelope)
    store.mark_applied(envelope)
    return store, organization, agent


class Runtime:
    def __init__(self, container="running"):
        self.container = container
        self.events = []
        self.locks = {}

    def lock(self, agent_id):
        return self.locks.setdefault(agent_id, asyncio.Lock())

    async def image_is_current(self, organization_id, agent_id):
        return True

    async def assert_quiet(self, organization_id, agent_id):
        return None

    async def inspect(self, organization_id, agent_id):
        if self.container is None:
            return None
        return {
            "state": {
                "Running": self.container == "running",
                "Status": self.container,
            }
        }

    async def start(self, organization_id, agent_id):
        self.events.append("start")
        if self.container is None:
            raise RuntimeUnavailable("missing")
        self.container = "running"

    async def stop(self, organization_id, agent_id):
        self.events.append("stop")
        if self.container is None:
            raise RuntimeUnavailable("missing")
        self.container = "exited"

    async def restart(self, organization_id, agent_id):
        self.events.append("restart")
        self.container = "running"

    async def rebuild(self, organization_id, agent_id):
        self.events.append("rebuild")
        self.container = "running"


class Dispatcher:
    def __init__(self, activity="", raced_activity=None, settled=True):
        self.activity = activity
        self.raced_activity = raced_activity
        self.settled = settled
        self.activity_calls = 0
        self.quiesced = 0
        self.reconciled = 0

    async def agent_active(self, organization_id, agent_id):
        return bool(self.activity)

    async def agent_activity(self, organization_id, agent_id):
        self.activity_calls += 1
        if self.activity_calls > 1 and self.raced_activity is not None:
            return self.raced_activity
        return self.activity

    async def quiesce_agent(self, organization_id, agent_id, author):
        self.quiesced += 1
        self.activity = ""

    async def reconcile_agent_effects(self, organization_id, agent_id):
        self.reconciled += 1

    def agent_effects_settled(self, organization_id, agent_id):
        return self.settled


class Configuration:
    async def apply_agent(self, organization_id, agent_id):
        return "applied"

    async def refresh_runtime(self, organization_id, agent_id):
        return None


def test_runtime_rollout_replaces_only_stale_running_agents_and_retains_maintenance(tmp_path):
    store, organization, agent = _applied_agent(tmp_path)

    class ImageRuntime(Runtime):
        stale = True

        async def image_is_current(self, organization_id, agent_id):
            return not self.stale

        async def assert_quiet(self, organization_id, agent_id):
            assert store.maintenance_status() == {"state": "closed"}

        async def rebuild(self, organization_id, agent_id):
            await super().rebuild(organization_id, agent_id)
            self.stale = False

    class RolloutConfiguration(Configuration):
        async def refresh_runtime(self, organization_id, agent_id):
            assert store.maintenance_status() == {"state": "closed"}
            runtime.events.append("configure")

    runtime = ImageRuntime()
    lifecycle = AgentLifecycle(store, runtime, Dispatcher(), RolloutConfiguration())

    async def check():
        with pytest.raises(ValueError, match="maintenance"):
            await lifecycle.rollout_runtime(organization, agent)
        assert runtime.events == []
        assert store.close_maintenance_admission()
        assert await lifecycle.rollout_runtime(organization, agent) is True
        assert runtime.events == ["rebuild", "configure"]
        assert (await lifecycle.status(organization, agent))["lifecycle_state"] == "running"
        assert await lifecycle.rollout_runtime(organization, agent) is False
        runtime.stale = True
        runtime.container = "exited"
        store.set_lifecycle_state(organization, agent, desired="stopped", state="stopped")
        assert await lifecycle.rollout_runtime(organization, agent) is False
        assert runtime.events == ["rebuild", "configure"]
        assert store.maintenance_status() == {"state": "closed"}

    asyncio.run(check())


def test_start_updates_stopped_image_but_never_replaces_unsettled_effects(tmp_path):
    store, organization, agent = _applied_agent(tmp_path)
    store.set_lifecycle_state(organization, agent, desired="stopped", state="stopped")

    class StaleRuntime(Runtime):
        async def image_is_current(self, organization_id, agent_id):
            return False

    runtime = StaleRuntime(container="exited")
    dispatcher = Dispatcher(settled=False)
    lifecycle = AgentLifecycle(store, runtime, dispatcher, Configuration())
    request = HostLifecycleRequest(
        action="start", author=Actor(kind="human", id=uuid4(), name="Owner")
    )
    with pytest.raises(RuntimeUnavailable, match="reconciliation"):
        asyncio.run(lifecycle.perform(organization, agent, request))
    assert runtime.events == []
    dispatcher.settled = True
    result = asyncio.run(lifecycle.perform(organization, agent, request))
    assert runtime.events == ["rebuild"]
    assert result["lifecycle_state"] == "running"


def test_image_identity_uses_immutable_id_and_rejects_missing_image_evidence(tmp_path):
    store, organization, agent = _applied_agent(tmp_path)

    class ImageRuntime(DockerRuntime):
        selected = "sha256:" + "a" * 64
        actual = "sha256:" + "a" * 64

        async def inspect(self, organization_id, agent_id):
            return {"image": self.actual}

        async def docker(self, *args, content=None):
            if args == ("image", "inspect", "--format", "{{.Parent}}", self.actual):
                return b""
            assert args == ("image", "inspect", "--format", "{{.Id}}", "new-release-tag")
            return self.selected.encode()

    runtime = ImageRuntime(store, "http://127.0.0.1:1", image="new-release-tag")
    assert asyncio.run(runtime.image_is_current(organization, agent)) is True
    runtime.actual = "sha256:" + "b" * 64
    assert asyncio.run(runtime.image_is_current(organization, agent)) is False
    runtime.selected = ""
    with pytest.raises(RuntimeUnavailable, match="image identity"):
        asyncio.run(runtime.image_is_current(organization, agent))


@pytest.mark.parametrize("failure", ["rebuild", "configuration", "reconciliation", "cancelled"])
def test_failed_rollout_preserves_recovery_state_and_closed_admission(tmp_path, failure):
    store, organization, agent = _applied_agent(tmp_path)
    store.close_maintenance_admission()

    class StaleRuntime(Runtime):
        async def image_is_current(self, organization_id, agent_id):
            return False

        async def rebuild(self, organization_id, agent_id):
            if failure == "cancelled":
                raise asyncio.CancelledError
            if failure == "rebuild":
                raise RuntimeUnavailable("replacement failed")
            await super().rebuild(organization_id, agent_id)

    class RolloutConfiguration(Configuration):
        async def refresh_runtime(self, organization_id, agent_id):
            if failure == "configuration":
                raise RuntimeUnavailable("configuration failed")
            if failure == "reconciliation":
                dispatcher.settled = False

    dispatcher = Dispatcher()
    lifecycle = AgentLifecycle(store, StaleRuntime(), dispatcher, RolloutConfiguration())
    with pytest.raises(asyncio.CancelledError if failure == "cancelled" else RuntimeUnavailable):
        asyncio.run(lifecycle.rollout_runtime(organization, agent))
    assert store.agent_status(organization, agent)["lifecycle_state"] == "recovery_required"
    assert store.maintenance_status() == {"state": "closed"}


def test_checkpoint_image_keeps_its_underlying_runtime_identity(tmp_path):
    store, organization, agent = _applied_agent(tmp_path)
    base = "sha256:" + "a" * 64
    checkpoint = "sha256:" + "b" * 64
    nested = "sha256:" + "c" * 64

    class CheckpointRuntime(DockerRuntime):
        selected = base

        async def inspect(self, organization_id, agent_id):
            return {"image": nested}

        async def docker(self, *args, content=None):
            assert args[:3] == ("image", "inspect", "--format")
            if args[3] == "{{.Id}}":
                return self.selected.encode()
            assert args[3] == "{{.Parent}}"
            return {nested: checkpoint, checkpoint: base, base: ""}[args[4]].encode()

    runtime = CheckpointRuntime(store, "http://127.0.0.1:1", image="selected-image")
    assert asyncio.run(runtime.image_is_current(organization, agent)) is True
    runtime.selected = "sha256:" + "d" * 64
    assert asyncio.run(runtime.image_is_current(organization, agent)) is False
