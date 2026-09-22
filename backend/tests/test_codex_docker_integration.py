"""Credential-free native Codex proof through Fesnyng's actual runtime boundary."""

import asyncio
import os
import secrets
import signal
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest

from docker_proof import _defer_proof_interrupts, _dispose_proof, _run_proof
from fesnyng_backend.agent_models import AgentConfiguration, NativeSkill
from fesnyng_backend.host_dispatch import Dispatcher, DispatchStore, Submission
from fesnyng_backend.host_interactions import Interactions
from fesnyng_backend.host_models import Actor, HostAgentConfiguration
from fesnyng_backend.host_runtime import DockerRuntime, RuntimeUnavailable
from fesnyng_backend.host_store import HostStore
from fesnyng_backend.settings import ServiceSettings


@pytest.mark.skipif(
    os.environ.get("FESNYNG_CODEX_DOCKER_TESTS") != "true",
    reason="Requires the pinned Codex Docker image",
)
def test_codex_dispatch_interrupt_and_reconnect_preserve_native_history():
    directory = Path(tempfile.mkdtemp(prefix="fesnyng-codex-integration-"))
    store = HostStore(
        ServiceSettings(
            service="agent-host",
            state_directory=directory,
            database_path=directory / "host.sqlite3",
        )
    )
    store.initialize()
    org, agent = str(uuid4()), str(uuid4())
    store.bind_organization(org, secrets.token_urlsafe(32))
    envelope = HostAgentConfiguration(
        host_id=store.instance_id,
        organization_id=org,
        agent_id=agent,
        version=1,
        name="Codex integration agent",
        configuration=AgentConfiguration(
            runtime_type="codex",
            reasoning_effort="high",
            instructions="Use only the assigned thread directory.",
            skills=[NativeSkill(name="proof", content="Report the current directory.")],
        ),
    )
    store.stage_agent(envelope)
    runtime = DockerRuntime(
        store,
        "http://127.0.0.1:1",
        image=os.environ.get("FESNYNG_CODEX_TEST_IMAGE", "fesnyng-agent:local"),
    )
    print(f"Isolated Codex proof: state={directory}, container={runtime.name(agent)}")
    dispatches = DispatchStore(store)
    dispatches.initialize()
    interactions = Interactions(store, runtime)
    interactions.initialize()
    dispatcher = Dispatcher(dispatches, runtime, interactions=interactions)
    author = Actor(kind="human", id=uuid4(), name="Integration owner")

    async def reconcile(session_id):
        await dispatcher._thread(
            (org, agent, session_id), dispatches.for_thread(org, agent, session_id)
        )
        await asyncio.gather(*dispatcher.tasks.values())

    async def exercise():
        try:
            await runtime.configure(envelope)
            store.mark_applied(envelope)
            session = await runtime.create_session(org, agent, "Native proof", "default")
            session_id = session["id"]
            assert store.session(org, agent, session_id)["runtime_type"] == "codex"
            delivery = dispatches.enqueue(
                org,
                agent,
                session_id,
                Submission(id=uuid4(), text="Say hello. Do not use tools."),
                author,
            )
            await reconcile(session_id)
            receipt = dispatches.get(org, agent, delivery["id"])
            assert receipt["state"] == "active", receipt
            steering = dispatches.enqueue(
                org,
                agent,
                session_id,
                Submission(id=uuid4(), mode="steering", text="Keep the reply brief."),
                author,
            )
            await reconcile(session_id)
            assert dispatches.get(org, agent, steering["id"])["state"] == "active"
            queued = dispatches.enqueue(
                org,
                agent,
                session_id,
                Submission(id=uuid4(), text="This queued message will be cancelled."),
                author,
            )
            await reconcile(session_id)
            assert dispatches.get(org, agent, queued["id"])["state"] == "queued"
            stop = dispatches.enqueue(
                org,
                agent,
                session_id,
                Submission(id=uuid4(), mode="stop", cancel_queued=True),
                author,
            )
            for _ in range(30):
                await reconcile(session_id)
                if dispatches.get(org, agent, stop["id"])["state"] == "completed":
                    break
                await asyncio.sleep(0.1)
            assert dispatches.get(org, agent, stop["id"])["state"] == "completed"
            assert dispatches.get(org, agent, queued["id"])["state"] == "cancelled"
            await runtime.codex.transport.close()
            history = await runtime.codex.call(
                org, agent, "thread/read", {"threadId": session_id, "includeTurns": True}
            )
            turns = history["thread"]["turns"]
            assert turns and turns[0]["status"] in {"interrupted", "failed", "completed"}
            assert history["thread"]["reasoningEffort"] == "high"
            assert any(
                item.get("clientId") == delivery["id"]
                for turn in turns
                for item in turn["items"]
                if item["type"] == "userMessage"
            )
            default = envelope.model_copy(
                update={
                    "version": 2,
                    "configuration": envelope.configuration.model_copy(
                        update={"reasoning_effort": None}
                    ),
                }
            )
            store.stage_agent(default)
            await runtime.configure(default)
            store.mark_applied(default)
            reset = dispatches.enqueue(
                org,
                agent,
                session_id,
                Submission(id=uuid4(), text="Use the model default."),
                author,
            )
            await reconcile(session_id)
            assert dispatches.get(org, agent, reset["id"])["state"] == "active"
            default_history = await runtime.codex.call(
                org, agent, "thread/read", {"threadId": session_id, "includeTurns": True}
            )
            assert default_history["thread"]["reasoningEffort"] == "low"
            assert any(
                item.get("clientId") == reset["id"]
                for turn in default_history["thread"]["turns"]
                for item in turn["items"]
                if item["type"] == "userMessage"
            )
            reset_stop = dispatches.enqueue(
                org,
                agent,
                session_id,
                Submission(id=uuid4(), mode="stop", cancel_queued=True),
                author,
            )
            for _ in range(30):
                await reconcile(session_id)
                if dispatches.get(org, agent, reset_stop["id"])["state"] == "completed":
                    break
                await asyncio.sleep(0.1)
            assert dispatches.get(org, agent, reset_stop["id"])["state"] == "completed"
            await runtime.assert_quiet(org, agent)
            # Native session creation owns root-created children in the employee
            # bind. Empty only this proof's bind through that running owner so
            # the shared fixture teardown can remove its temporary host state.
            await runtime.docker(
                "exec",
                runtime.name(agent),
                "sh",
                "-ceu",
                'find "$1" -mindepth 1 -depth -delete',
                "codex-integration-proof",
                str(runtime.employee_workspace_root(org, agent)),
            )
            await runtime.stop(org, agent)
            with pytest.raises(RuntimeUnavailable, match="not running"):
                await runtime.codex.call(
                    org, agent, "thread/read", {"threadId": session_id, "includeTurns": True}
                )
            stopped = await runtime.inspect(org, agent)
            assert stopped is not None and not stopped["state"]["Running"]
        finally:
            await runtime.codex.transport.close()

    async def bounded_exercise():
        async with asyncio.timeout(90):
            await exercise()

    verified = False
    received_interrupts: list[int] = []
    try:
        _run_proof(bounded_exercise(), received_interrupts)
        verified = True
    finally:
        # _run_proof keeps signals deferred while Runner drains executor Docker calls.
        with _defer_proof_interrupts() as deferred_interrupts:
            cleaned = asyncio.run(_dispose_proof(runtime, org, agent, directory, verified=verified))
        received_interrupts.extend(deferred_interrupts)
        if received_interrupts:
            names = ", ".join(signal.Signals(signum).name for signum in received_interrupts)
            print(
                f"Docker proof teardown deferred {names} until cleanup completed.",
                file=sys.stderr,
                flush=True,
            )
        if verified and not cleaned:
            raise RuntimeError("Verified Docker proof fixture cleanup failed")
        if verified and received_interrupts:
            raise KeyboardInterrupt("Docker proof interrupted during teardown")
