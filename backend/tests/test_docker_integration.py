"""Real native-runtime gate; opt in after building agent-runtime/Dockerfile."""

import asyncio
import os
import secrets
import shutil
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest

from fesnyng_backend.agent_models import AgentConfiguration, NativeSkill
from fesnyng_backend.host_models import HostAgentConfiguration, permission_rules
from fesnyng_backend.host_runtime import DockerRuntime, RuntimeUnavailable
from fesnyng_backend.host_store import HostStore
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

    async def check():
        runtime = DockerRuntime(store, "http://127.0.0.1:1")
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
        # Only successfully verified, test-created resources are removed. Failures retain evidence.
        await runtime.inspect(org, agent)
        await runtime.docker("stop", runtime.name(agent))
        await runtime.docker("rm", runtime.name(agent))
        with pytest.raises(RuntimeUnavailable, match="checkpoint"):
            await runtime.ensure(org, agent)
        await runtime.docker(
            "volume", "rm", runtime.name(agent) + "-home", runtime.name(agent) + "-workspace"
        )
        await runtime.docker("image", "rm", store.agent(org, agent)["snapshot_image"])
        shutil.rmtree(tmp_path)

    asyncio.run(check())
