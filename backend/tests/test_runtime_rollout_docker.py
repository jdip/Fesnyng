"""Real image rollout through maintenance and lifecycle with retained native state."""

import asyncio
import os
import secrets
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest

from docker_proof import _defer_proof_interrupts, _dispose_proof, _run_proof
from fesnyng_backend.agent_host import create_app
from fesnyng_backend.agent_lifecycle import HostLifecycleRequest
from fesnyng_backend.agent_models import AgentConfiguration
from fesnyng_backend.host_models import Actor, HostAgentConfiguration
from fesnyng_backend.settings import ServiceSettings


@pytest.mark.skipif(
    os.environ.get("FESNYNG_ROLLOUT_DOCKER_TESTS") != "true",
    reason="Requires old and new runtime Docker images",
)
@pytest.mark.parametrize("harness", ["opencode", "codex"])
def test_rollout_and_deferred_start_preserve_native_state(harness):
    directory = Path(tempfile.mkdtemp(prefix="fesnyng-rollout-proof-"))
    app = create_app(
        ServiceSettings(
            service="agent-host",
            state_directory=directory,
            database_path=directory / "host.sqlite3",
        )
    )
    store, runtime = app.state.host_store, app.state.host_runtime
    org, agent = str(uuid4()), str(uuid4())
    store.bind_organization(org, secrets.token_urlsafe(32))
    envelope = HostAgentConfiguration(
        host_id=store.instance_id,
        organization_id=org,
        agent_id=agent,
        version=1,
        name="Rollout proof",
        configuration=AgentConfiguration(runtime_type=harness),
    )
    store.stage_agent(envelope)
    old_image = os.environ["FESNYNG_ROLLOUT_OLD_IMAGE"]
    new_image = os.environ["FESNYNG_ROLLOUT_NEW_IMAGE"]
    runtime.image = old_image
    lifecycle, guard = app.state.agent_lifecycle, app.state.maintenance_guard
    owner = Actor(kind="human", id=uuid4(), name="Proof owner")
    print(f"Rollout proof: state={directory}, container={runtime.name(agent)}", flush=True)

    async def container_id():
        return await runtime.docker("inspect", "--format", "{{.Id}}", runtime.name(agent))

    async def exercise():
        try:
            await runtime.configure(envelope)
            store.mark_applied(envelope)
            session = await runtime.create_session(org, agent, "Retained history", "default")
            thread_id = session["id"]
            await runtime.write_file(org, agent, "/home/agent/rollout-proof.txt", "retained home")
            await runtime.write_file(
                org, agent, "/workspace/rollout-proof.txt", "retained workspace"
            )
            await runtime.write_file(org, agent, "/opt/rollout-checkpoint.txt", "retained layer")
            await runtime.replace(org, agent)
            original = await container_id()
            await guard.acquire()
            assert await guard.rollout() == {"state": "closed", "updated": 0}
            assert await container_id() == original
            assert (
                await runtime.docker(
                    "exec", runtime.name(agent), "cat", "/opt/rollout-checkpoint.txt"
                )
                == b"retained layer"
            )
            runtime.image = new_image
            assert await guard.rollout() == {"state": "closed", "updated": 1}
            replacement = await container_id()
            assert replacement != original
            assert await runtime.image_is_current(org, agent)
            runtime.image = os.environ["FESNYNG_ROLLOUT_EQUIVALENT_IMAGE"]
            assert await guard.rollout() == {"state": "closed", "updated": 0}
            assert await container_id() == replacement
            assert store.agent_status(org, agent)["lifecycle_state"] == "running"
            assert store.session(org, agent, thread_id)["runtime_type"] == harness
            for root, expected in (
                ("/home/agent", b"retained home"),
                ("/workspace", b"retained workspace"),
            ):
                assert (
                    await runtime.docker(
                        "exec", runtime.name(agent), "cat", f"{root}/rollout-proof.txt"
                    )
                    == expected
                )
            if harness == "codex":
                history = await runtime.codex.call(
                    org, agent, "thread/read", {"threadId": thread_id, "includeTurns": False}
                )
                assert history["thread"]["id"] == thread_id
                models = await runtime.codex.call(org, agent, "model/list", {})
                assert isinstance(models["data"], list) and models["data"]
                version = await runtime.docker(
                    "exec", runtime.name(agent), "/opt/fesnyng/node_modules/.bin/codex", "--version"
                )
                assert b"0.156.1" in version
            else:
                history = await runtime.request(
                    org,
                    agent,
                    f"/session/{thread_id}",
                    directory=store.session(org, agent, thread_id)["directory"],
                )
                assert history["id"] == thread_id
            await guard.release()
            await lifecycle.perform(
                org, agent, HostLifecycleRequest(action="stop", confirmed=True, author=owner)
            )
            runtime.image = old_image
            await guard.acquire()
            assert await guard.rollout() == {"state": "closed", "updated": 0}
            assert await container_id() == replacement
            assert not (await runtime.inspect(org, agent))["state"]["Running"]
            await guard.release()
            await lifecycle.perform(org, agent, HostLifecycleRequest(action="start", author=owner))
            assert await container_id() != replacement
            assert await runtime.image_is_current(org, agent)
            assert store.session(org, agent, thread_id)["runtime_type"] == harness
        finally:
            await runtime.codex.transport.close()

    async def bounded():
        async with asyncio.timeout(300):
            await exercise()

    verified = False
    interrupts = []
    try:
        _run_proof(bounded(), interrupts)
        verified = True
    finally:
        with _defer_proof_interrupts() as deferred:
            cleaned = asyncio.run(
                _dispose_proof(
                    runtime,
                    org,
                    agent,
                    directory,
                    verified=verified,
                    snapshot_image=store.agent(org, agent)["snapshot_image"],
                )
            )
        interrupts.extend(deferred)
        if verified and not cleaned:
            raise RuntimeError("Rollout proof cleanup failed")
        if interrupts:
            raise KeyboardInterrupt("Rollout proof interrupted")
