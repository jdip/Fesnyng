"""Real, credential-free harness replacement and host-owned history proof."""

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
from fesnyng_backend.agent_models import AgentConfiguration
from fesnyng_backend.host_configuration import HostConfiguration
from fesnyng_backend.host_credentials import CredentialStore
from fesnyng_backend.host_dispatch import DispatchStore
from fesnyng_backend.host_interactions import Interactions
from fesnyng_backend.host_models import HostAgentConfiguration
from fesnyng_backend.host_runtime import DockerRuntime
from fesnyng_backend.host_store import HostStore
from fesnyng_backend.host_workspace import Workspace
from fesnyng_backend.settings import ServiceSettings


@pytest.mark.skipif(
    os.environ.get("FESNYNG_CODEX_DOCKER_TESTS") != "true",
    reason="Requires both pinned native harnesses in Docker",
)
def test_native_switch_both_directions_keeps_original_threads_frozen():
    directory = Path(tempfile.mkdtemp(prefix="fesnyng-switch-integration-"))
    host = HostStore(
        ServiceSettings(
            service="agent-host",
            state_directory=directory,
            database_path=directory / "host.sqlite3",
        )
    )
    host.initialize()
    org, agent = str(uuid4()), str(uuid4())
    host.bind_organization(org, secrets.token_urlsafe(32))
    runtime = DockerRuntime(
        host,
        "http://127.0.0.1:1",
        image=os.environ.get("FESNYNG_CODEX_TEST_IMAGE", "fesnyng-agent:local"),
    )
    credentials = CredentialStore(directory / "credentials.sqlite3")
    credentials.initialize()
    dispatches = DispatchStore(host)
    dispatches.initialize()
    interactions = Interactions(host, runtime)
    interactions.initialize()
    configuration = HostConfiguration(host, runtime, credentials, interactions, dispatches)
    workspace = Workspace(host, runtime, dispatches, interactions)
    envelope = HostAgentConfiguration(
        host_id=host.instance_id,
        organization_id=org,
        agent_id=agent,
        version=1,
        name="Switch proof",
    )
    print(f"Isolated switch proof: state={directory}, container={runtime.name(agent)}", flush=True)

    async def exercise():
        try:
            await configuration.apply(envelope)
            original = await runtime.create_session(org, agent, "Original", "default")
            await runtime.write_file(
                org, agent, "/workspace/retained-proof.txt", "retained across harnesses"
            )
            await configuration.switch_harness(org, agent, 1, "codex")
            codex_envelope = envelope.model_copy(
                update={"version": 2, "configuration": AgentConfiguration(runtime_type="codex")}
            )
            await configuration.apply(codex_envelope)
            assert host.agent_status(org, agent)["applied_version"] == 2
            assert (await workspace.get(org, agent, original["id"]))["frozen"] is True
            assert await workspace.messages(org, agent, original["id"]) == []
            codex = await runtime.create_session(org, agent, "Codex", "default")
            await configuration.switch_harness(org, agent, 2, "opencode")
            await configuration.apply(envelope.model_copy(update={"version": 3}))
            for thread in (original, codex):
                with pytest.raises(ValueError, match="permanently frozen"):
                    host.require_writable(org, agent, thread["id"])
            snapshot = host.frozen_snapshot(org, agent, codex["id"])
            assert snapshot is not None and snapshot["history"]["turns"] == []
            assert (
                await runtime.docker(
                    "exec", runtime.name(agent), "cat", "/workspace/retained-proof.txt"
                )
                == b"retained across harnesses"
            )
            fresh = await runtime.create_session(org, agent, "Fresh", "default")
            assert fresh["id"] not in {original["id"], codex["id"]}
        finally:
            await runtime.codex.transport.close()

    async def bounded():
        async with asyncio.timeout(120):
            await exercise()

    verified = False
    received_interrupts: list[int] = []
    try:
        _run_proof(bounded(), received_interrupts)
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
