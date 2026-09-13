"""Real Docker verification for the scoped thread Git-context read."""

import asyncio
import os
import secrets
import shutil
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest

from fesnyng_backend.agent_models import AgentConfiguration
from fesnyng_backend.host_models import HostAgentConfiguration
from fesnyng_backend.host_runtime import DockerRuntime, RuntimeUnavailable, _workspace_context
from fesnyng_backend.host_store import HostStore
from fesnyng_backend.settings import ServiceSettings


def test_workspace_context_receipt_preserves_known_repository_when_git_filters_are_configured():
    context = _workspace_context(
        b"available\0example-project\0available\0main\0unavailable\0\0\0\0\0configured_filter\0"
    )

    assert context == {
        "repository": {"state": "available", "name": "example-project"},
        "branch": {"state": "available", "name": "main"},
        "changes": {"state": "unavailable", "reason": "configured_filter"},
    }


@pytest.mark.skipif(
    os.environ.get("FESNYNG_DOCKER_TESTS") != "true",
    reason="Requires the real built Docker runtime",
)
def test_workspace_context_reads_one_mapped_repository_with_real_git_changes():
    """Staged and unstaged text, binary, and untracked changes have distinct totals."""
    tmp_path = Path(tempfile.mkdtemp(prefix="fesnyng-docker-context-"))
    store = HostStore(
        ServiceSettings(
            service="agent-host",
            state_directory=tmp_path / "state",
            database_path=tmp_path / "state" / "host.sqlite3",
        )
    )
    store.initialize()
    org, agent = str(uuid4()), str(uuid4())
    runtime: DockerRuntime | None = None
    store.bind_organization(org, secrets.token_urlsafe(32))
    envelope = HostAgentConfiguration(
        host_id=store.instance_id,
        organization_id=org,
        agent_id=agent,
        version=1,
        name="Context integration agent",
        configuration=AgentConfiguration(workspace="default"),
    )
    store.stage_agent(envelope)

    async def check():
        nonlocal runtime
        runtime = DockerRuntime(
            store,
            "http://127.0.0.1:1",
            os.environ.get("FESNYNG_AGENT_HOST_IMAGE", "fesnyng-agent:local"),
        )
        await runtime.configure(envelope)
        store.mark_applied(envelope)
        directory = "/workspace/default/threads/example-project"
        await runtime.docker(
            "exec",
            runtime.name(agent),
            "sh",
            "-c",
            '''mkdir -p "$1"
git -C "$1" init -q
git -C "$1" config user.email test@example.invalid
git -C "$1" config user.name "Context test"
git -C "$1" remote add origin https://github.com/example-org/context-origin.git
printf "*.bin binary\\n" > "$1/.gitattributes"
printf "filtered.txt filter=marker\\n" >> "$1/.gitattributes"
printf "one\\n" > "$1/added.txt"
printf "remove\\nkeep\\n" > "$1/deleted.txt"
printf "old" > "$1/change.bin"
printf "original\\n" > "$1/filtered.txt"
old_name="$(printf '7\\t9')"
printf "rename\\n" > "$1/$old_name"
git -C "$1" add . && git -C "$1" commit -qm initial
new_name="$(printf -- '-\\t-')"
git -C "$1" mv "$old_name" "$new_name"
git -C "$1" config diff.renames true
printf "one\\ntwo\\n" > "$1/added.txt"
git -C "$1" add added.txt
printf "three\\n" >> "$1/added.txt"
printf "keep\\n" > "$1/deleted.txt"
printf "new" > "$1/change.bin"
printf one > "$1/untracked-one"
printf two > "$1/untracked-two"''',
            "workspace-context-fixture",
            directory,
        )
        context = await runtime.workspace_context(org, agent, directory)
        assert context["repository"] == {"state": "available", "name": "context-origin"}
        assert context["branch"]["state"] == "available"
        assert context["branch"]["name"]
        assert context["changes"] == {
            "state": "available",
            "added": 2,
            "deleted": 1,
            "binaryFiles": 1,
            "untracked": 2,
        }
        await runtime.docker(
            "exec",
            runtime.name(agent),
            "sh",
            "-c",
            """printf "changed\\n" > "$1/filtered.txt"
git -C "$1" config filter.marker.clean 'sh -c "touch /tmp/fesnyng-context-filter-marker; cat"'""",
            "workspace-context-filter",
            directory,
        )
        filtered = await runtime.workspace_context(org, agent, directory)
        assert filtered["changes"] == {"state": "unavailable", "reason": "configured_filter"}
        await runtime.docker(
            "exec",
            runtime.name(agent),
            "test",
            "!",
            "-e",
            "/tmp/fesnyng-context-filter-marker",
        )
        await runtime.docker(
            "exec",
            runtime.name(agent),
            "sh",
            "-c",
            'mkdir -p "$1/broken/.git" "$1/escaped"; git init --separate-git-dir /tmp/escaped-git "$1/escaped" -q',
            "workspace-context-invalid-fixtures",
            "/workspace/default/threads",
        )
        with pytest.raises(RuntimeUnavailable):
            await runtime.workspace_context(org, agent, "/workspace/default/threads/broken")
        with pytest.raises(RuntimeUnavailable):
            await runtime.workspace_context(org, agent, "/workspace/default/threads/escaped")
        await runtime.inspect(org, agent)
        await runtime.docker("stop", runtime.name(agent))
        await runtime.docker("rm", runtime.name(agent))
        await runtime.docker(
            "volume", "rm", runtime.name(agent) + "-home", runtime.name(agent) + "-workspace"
        )
        shutil.rmtree(tmp_path)

    try:
        asyncio.run(check())
    except BaseException:

        async def stop_failed_runtime() -> None:
            if runtime is None:
                return
            try:
                container = await runtime.inspect(org, agent)
                if container is not None and container["state"]["Running"]:
                    await runtime.docker("stop", runtime.name(agent))
            except RuntimeUnavailable:
                return

        asyncio.run(stop_failed_runtime())
        raise
