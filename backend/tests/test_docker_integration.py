"""Real native-runtime gate; opt in after building agent-runtime/Dockerfile."""

import asyncio
import base64
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
from fesnyng_backend.host_dispatch import DispatchStore
from fesnyng_backend.host_interactions import Interactions
from fesnyng_backend.host_models import HostAgentConfiguration, permission_rules
from fesnyng_backend.host_runtime import DockerRuntime, RuntimeUnavailable
from fesnyng_backend.host_store import HostStore
from fesnyng_backend.host_workspace import Workspace
from fesnyng_backend.settings import ServiceSettings


@pytest.mark.skipif(
    os.environ.get("FESNYNG_DOCKER_TESTS") != "true",
    reason="Requires the real built Docker runtime",
)
def test_native_configuration_and_replacement_preserve_agent_state():
    # A failed real-runtime proof must outlive pytest's rotating temporary directories.
    tmp_path = Path(tempfile.mkdtemp(prefix="fesnyng-docker-"))
    store = HostStore(
        ServiceSettings(
            service="agent-host",
            state_directory=tmp_path / "state",
            database_path=tmp_path / "state" / "host.sqlite3",
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
        name="Integration agent",
        configuration=AgentConfiguration(
            instructions="Continue only in the assigned thread workspace.",
            skills=[
                NativeSkill(name="retained", content="Retained reusable instruction"),
                NativeSkill(name="removed", content="Removed reusable instruction"),
                NativeSkill(
                    name="run", content="Retained explicit instruction", explicit_only=True
                ),
                NativeSkill(
                    name="obsolete", content="Removed explicit instruction", explicit_only=True
                ),
            ],
        ),
    )
    store.stage_agent(envelope)
    runtime = DockerRuntime(store, "http://127.0.0.1:1")
    print(
        f"Isolated Docker proof: state={tmp_path}, container={runtime.name(agent)}",
        flush=True,
    )

    async def check():
        await runtime.configure(envelope)
        store.mark_applied(envelope)
        managed_instructions = (
            await runtime.docker("exec", runtime.name(agent), "cat", "/home/agent/AGENTS.md")
        ).decode()
        assert managed_instructions.startswith(
            "Use the working directory in the current native environment as this thread's workspace."
        )
        assert managed_instructions.endswith(envelope.configuration.instructions)
        native = await runtime.request(org, agent, "/config")
        assert native.get("model") == "openai/gpt-6-astra", (
            "Native global configuration was not loaded"
        )
        assert "file:///opt/fesnyng/host-auth.mjs" in native.get("plugin", [])
        assert (await runtime.request(org, agent, "/provider/auth")).get("openai") == []
        commands = {command["name"] for command in await runtime.request(org, agent, "/command")}
        assert {"fesnyng/run", "fesnyng/obsolete"} <= commands
        await runtime.request(
            org,
            agent,
            "/global/config",
            method="PATCH",
            body={
                "command": {"unrelated": {"template": "Keep this native command"}},
                "plugin": [["file:///opt/fesnyng/host-auth.mjs", {"retained": True}]],
            },
        )
        updated = envelope.model_copy(
            update={
                "version": 2,
                "configuration": AgentConfiguration(
                    skills=[envelope.configuration.skills[0], envelope.configuration.skills[2]],
                ),
            }
        )
        store.stage_agent(updated)
        await runtime.configure(updated)
        store.mark_applied(updated)
        assert ["file:///opt/fesnyng/host-auth.mjs", {"retained": True}] in (
            await runtime.request(org, agent, "/global/config")
        )["plugin"]
        commands = {command["name"] for command in await runtime.request(org, agent, "/command")}
        assert {"fesnyng/run", "unrelated"} <= commands
        assert "fesnyng/obsolete" not in commands
        skills = {skill["name"] for skill in await runtime.request(org, agent, "/skill")}
        assert "retained" in skills
        assert "removed" not in skills
        assert "run" not in skills
        session = await runtime.create_session(org, agent, "Retained thread", "default")
        saved = store.session(org, agent, session["id"])
        assert session["permission"] == permission_rules(updated)
        assert (await runtime.request(org, agent, "/global/config"))["permission"] == {"*": "allow"}

        await runtime.write_file(
            org, agent, "/usr/local/bin/integration-tool", "#!/bin/sh\necho retained\n"
        )
        await runtime.docker(
            "exec", runtime.name(agent), "chmod", "+x", "/usr/local/bin/integration-tool"
        )
        await runtime.write_file(org, agent, "/workspace/integration.txt", "retained workspace")
        await runtime.replace(org, agent)
        assert (
            await runtime.docker("exec", runtime.name(agent), "/usr/local/bin/integration-tool")
            == b"retained\n"
        )
        assert (
            await runtime.docker("exec", runtime.name(agent), "cat", "/workspace/integration.txt")
            == b"retained workspace"
        )
        retained = await runtime.request(
            org, agent, f"/session/{session['id']}", directory=saved["directory"]
        )
        assert retained["id"] == session["id"]
        await runtime.stop(org, agent)
        stopped = await runtime.inspect(org, agent)
        assert stopped is not None and not stopped["state"]["Running"]
        await runtime.docker("rm", runtime.name(agent))
        with pytest.raises(RuntimeUnavailable, match="checkpoint"):
            await runtime.ensure(org, agent)
        return store.agent(org, agent)["snapshot_image"]

    verified = False
    snapshot_image: str | None = None
    received_interrupts: list[int] = []
    try:
        snapshot_image = _run_proof(check(), received_interrupts)
        verified = True
    finally:
        # _run_proof keeps signals deferred while Runner drains executor Docker calls.
        with _defer_proof_interrupts() as deferred_interrupts:
            cleaned = asyncio.run(
                _dispose_proof(
                    runtime,
                    org,
                    agent,
                    tmp_path,
                    verified=verified,
                    snapshot_image=snapshot_image,
                )
            )
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


@pytest.mark.skipif(
    os.environ.get("FESNYNG_DOCKER_TESTS") != "true",
    reason="Requires the real built Docker runtime",
)
def test_workspace_file_reads_are_scoped_and_byte_exact():
    tmp_path = Path(tempfile.mkdtemp(prefix="fesnyng-docker-files-"))
    store = HostStore(
        ServiceSettings(
            service="agent-host",
            state_directory=tmp_path / "state",
            database_path=tmp_path / "state" / "host.sqlite3",
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
        name="File integration agent",
    )
    store.stage_agent(envelope)
    runtime = DockerRuntime(store, "http://127.0.0.1:1")
    print(
        f"Isolated Docker file proof: state={tmp_path}, container={runtime.name(agent)}",
        flush=True,
    )

    async def read_download(workspace, session_id, path="sub/bytes.bin"):
        async with workspace.download(org, agent, session_id, path) as (_, stream):
            return b"".join([chunk async for chunk in stream])

    async def check():
        await runtime.configure(envelope)
        store.mark_applied(envelope)
        session = await runtime.create_session(org, agent, "Files", "default")
        saved = store.session(org, agent, session["id"])
        directory = saved["directory"]
        await runtime.docker(
            "exec",
            runtime.name(agent),
            "sh",
            "-c",
            'mkdir -p "$1/sub"; printf "  exact\\n\\tbytes\\0" > "$1/sub/bytes.bin"; printf x > "$1/root.txt"; ln -s /etc/passwd "$1/outside"; ln -s "$1/sub" "$1/internal"',
            "files",
            directory,
        )
        dispatches = DispatchStore(store)
        dispatches.initialize()
        workspace = Workspace(store, runtime, dispatches, Interactions(store, runtime))
        listing = await workspace.files(org, agent, session["id"], "")
        assert {entry["name"] for entry in listing["entries"]} >= {"sub", "root.txt"}
        assert "outside" not in {entry["name"] for entry in listing["entries"]}
        sub = await workspace.files(org, agent, session["id"], "sub/")
        assert sub["path"] == "sub" and sub["entries"][0]["name"] == "bytes.bin"
        internal = await workspace.files(org, agent, session["id"], "internal")
        assert internal["entries"][0]["name"] == "bytes.bin"
        assert await read_download(workspace, session["id"]) == b"  exact\n\tbytes\0"
        assert (
            await read_download(workspace, session["id"], "internal/bytes.bin")
            == b"  exact\n\tbytes\0"
        )
        preview = await workspace.preview(org, agent, session["id"], "sub/bytes.bin")
        assert preview["type"] == "binary" and preview["truncated"] is False
        with pytest.raises(RuntimeUnavailable):
            await workspace.preview(org, agent, session["id"], "outside")
        with pytest.raises(RuntimeUnavailable):
            await read_download(workspace, session["id"], "outside")
        await runtime.docker("exec", runtime.name(agent), "mkfifo", f"{directory}/pipe")
        with pytest.raises(RuntimeUnavailable):
            await asyncio.wait_for(workspace.preview(org, agent, session["id"], "pipe"), timeout=10)
        await runtime.docker(
            "exec",
            runtime.name(agent),
            "sh",
            "-c",
            'dd if=/dev/zero of="$1/sub/large.bin" bs=1024 count=129 status=none',
            "large-preview",
            directory,
        )
        large = await asyncio.wait_for(
            workspace.preview(org, agent, session["id"], "sub/large.bin"), timeout=15
        )
        assert large["truncated"] is True
        assert len(base64.b64decode(large["content"])) == 128 * 1024
        await runtime.docker(
            "exec",
            runtime.name(agent),
            "sh",
            "-c",
            'rm -rf "$1"; ln -s /etc "$1"',
            "replace-root",
            directory,
        )
        with pytest.raises(RuntimeUnavailable):
            await workspace.files(org, agent, session["id"], "")

    verified = False
    received_interrupts: list[int] = []
    try:
        _run_proof(check(), received_interrupts)
        verified = True
    finally:
        # _run_proof keeps signals deferred while Runner drains executor Docker calls.
        with _defer_proof_interrupts() as deferred_interrupts:
            cleaned = asyncio.run(_dispose_proof(runtime, org, agent, tmp_path, verified=verified))
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
