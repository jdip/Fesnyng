"""Opt-in lifecycle proof through the real control, host, Docker and native boundaries."""

import asyncio
import os
import secrets
import tempfile
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from docker_proof import _defer_proof_interrupts, _dispose_proof, _run_proof
from fesnyng_backend import control_host_routes
from fesnyng_backend.agent_host import create_app as create_host
from fesnyng_backend.agent_models import AgentConfiguration
from fesnyng_backend.codex_history import full_turns
from fesnyng_backend.control_plane import create_app
from fesnyng_backend.host_client import HostClient
from fesnyng_backend.host_models import HostAgentConfiguration
from fesnyng_backend.settings import ControlPlaneSessionSettings, ServiceSettings


@pytest.mark.skipif(
    os.environ.get("FESNYNG_WORKSPACE_LIFECYCLE_DOCKER_TESTS") != "true",
    reason="Requires the pinned dual-harness image and Docker-shared workspace root",
)
@pytest.mark.parametrize("harness", ["opencode", "codex"])
def test_workspace_lifecycle_rechecks_real_workspace_evidence(organization, monkeypatch, harness):
    """Removal, discard and replacement retain one native identity and unrelated work."""
    settings, _control, owner, org, agents, _ = organization
    directory = Path(
        tempfile.mkdtemp(
            prefix="fesnyng-workspace-lifecycle-proof-",
            dir=os.environ.get("FESNYNG_WORKSPACE_LIFECYCLE_PROOF_ROOT"),
        )
    ).resolve()
    shared = directory / "workspaces"
    host_app = create_host(
        ServiceSettings(
            service="agent-host",
            state_directory=directory,
            database_path=directory / "host.db",
            workspace_root=shared,
        )
    )
    host, runtime = host_app.state.host_store, host_app.state.host_runtime
    token = secrets.token_urlsafe(32)
    host.bind_organization(org.id, token)
    agents.register_host(str(host.instance_id), "Lifecycle proof", "http://host.test", org.id)
    agents.set_host_credential(org.id, str(host.instance_id), token)
    agent = agents.create_agent(
        org.id,
        owner.id,
        {
            "name": "Lifecycle engineer",
            "host_id": str(host.instance_id),
            "configuration": {"runtime_type": harness},
        },
    )
    agent_id = str(agent["id"])
    envelope = HostAgentConfiguration(
        host_id=host.instance_id,
        organization_id=org.id,
        agent_id=agent_id,
        version=1,
        name="Lifecycle engineer",
        configuration=AgentConfiguration(runtime_type=harness),
    )
    monkeypatch.setattr(
        control_host_routes,
        "HostClient",
        lambda store: HostClient(store, transport=httpx.ASGITransport(host_app)),
    )
    origin = "https://workspace-lifecycle-proof.example"
    app = create_app(settings, ControlPlaneSessionSettings(allowed_origin=origin))
    print(
        f"Workspace lifecycle proof: state={directory}, container={runtime.name(agent_id)}",
        flush=True,
    )

    async def expectation(client, path):
        inspected = await client.get(path)
        assert inspected.status_code == 200, inspected.text
        receipt = inspected.json()
        assert receipt["state"] == "ready"
        assert receipt["workspace_id"] and isinstance(receipt["generation"], int)
        assert isinstance(receipt.get("safety_digest"), str) and len(receipt["safety_digest"]) == 64
        return receipt, {
            "workspace_id": receipt["workspace_id"],
            "generation": receipt["generation"],
            "safety_digest": receipt["safety_digest"],
        }

    async def exercise():
        try:
            await host_app.state.host_configuration.apply(envelope)
            await runtime.docker(
                "exec",
                runtime.name(agent_id),
                "sh",
                "-ceu",
                "git init -b main /home/agent/lifecycle-source; "
                "git -C /home/agent/lifecycle-source config user.name 'Lifecycle Proof'; "
                "git -C /home/agent/lifecycle-source config user.email proof@example.invalid; "
                "printf base > /home/agent/lifecycle-source/README; "
                "git -C /home/agent/lifecycle-source add README; "
                "git -C /home/agent/lifecycle-source commit -m base; "
                "git clone --bare /home/agent/lifecycle-source /home/agent/lifecycle.git; "
                "git --git-dir=/home/agent/lifecycle.git update-server-info",
            )
            await runtime.docker(
                "exec",
                "-d",
                runtime.name(agent_id),
                "node",
                "-e",
                "const http=require('node:http'),fs=require('node:fs'),path=require('node:path');"
                "http.createServer((q,r)=>{const p=path.resolve('/home/agent','.'+new URL(q.url,'http://x').pathname);"
                "if(!p.startsWith('/home/agent/lifecycle.git/')){r.writeHead(404);return r.end()}fs.readFile(p,(e,b)=>{r.writeHead(e?404:200);r.end(e?'missing':b)})}).listen(9419,'127.0.0.1')",
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url=origin, headers={"Origin": origin}
            ) as client:
                login = await client.post(
                    "/auth/login",
                    json={"login": "owner", "password": "correct horse battery staple"},
                )
                assert login.status_code == 200
                client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
                base = f"/organizations/{org.id}/agents/{agent_id}"
                native = f"{base}/{harness}/session"
                project = await client.post(
                    f"/organizations/{org.id}/projects",
                    json={
                        "name": "Repository lifecycle",
                        "target_repository_url": "http://127.0.0.1:9419/lifecycle.git",
                        "default_checkout_branch": "main",
                    },
                )
                assert project.status_code == 201, project.text
                repository = await client.post(
                    native,
                    json={
                        "title": "Repository",
                        "project_id": project.json()["id"],
                        "creation_id": str(uuid4()),
                    },
                )
                ordinary = await client.post(
                    native, json={"title": "Disposable", "creation_id": str(uuid4())}
                )
                retained = await client.post(
                    native, json={"title": "Retained", "creation_id": str(uuid4())}
                )
                assert repository.status_code == ordinary.status_code == retained.status_code == 201
                ordinary_session, retained_session = ordinary.json(), retained.json()
                ordinary_path, retained_path = (
                    Path(ordinary_session["directory"]),
                    Path(retained_session["directory"]),
                )
                assert ordinary_path.is_relative_to(shared / org.id / agent_id)
                assert retained_path.is_relative_to(shared / org.id / agent_id)
                ordinary_workspace = f"{base}/sessions/{ordinary_session['id']}/workspace"
                retained_workspace = f"{base}/sessions/{retained_session['id']}/workspace"
                if harness == "opencode":
                    await runtime.request(
                        org.id,
                        agent_id,
                        f"/session/{ordinary_session['id']}/message",
                        method="POST",
                        directory=ordinary_session["directory"],
                        body={
                            "noReply": True,
                            "parts": [{"type": "text", "text": "Workspace history proof"}],
                        },
                    )
                    before_removal = await client.get(f"{native}/{ordinary_session['id']}/message")
                    assert before_removal.status_code == 200, before_removal.text
                    assert "Workspace history proof" in before_removal.text
                else:
                    await runtime.codex.call(
                        org.id,
                        agent_id,
                        "turn/start",
                        {
                            "threadId": ordinary_session["id"],
                            "input": [{"type": "text", "text": "Workspace history proof"}],
                        },
                    )
                    before_removal_turns = await full_turns(
                        runtime.codex, org.id, agent_id, ordinary_session["id"]
                    )
                    assert "Workspace history proof" in str(before_removal_turns)
                archived = await client.patch(
                    f"{native}/{ordinary_session['id']}",
                    json={"time": {"archived": 1_789_268_000_000}},
                )
                assert archived.status_code == 200, archived.text
                assert ordinary_path.is_dir()
                restored = await client.patch(
                    f"{native}/{ordinary_session['id']}", json={"time": {"archived": None}}
                )
                assert restored.status_code == 200, restored.text
                archive_history_suffix = "message" if harness == "opencode" else "history"
                archive_history = await client.get(
                    f"{native}/{ordinary_session['id']}/{archive_history_suffix}"
                )
                assert archive_history.status_code == 200, archive_history.text
                assert "Workspace history proof" in archive_history.text
                repository_workspace = f"{base}/sessions/{repository.json()['id']}/workspace"
                repository_ready, repository_current = await expectation(
                    client, repository_workspace
                )
                assert repository_ready["kind"] == "repository"
                await runtime.docker(
                    "exec",
                    runtime.name(agent_id),
                    "sh",
                    "-ceu",
                    'git -C "$1" remote remove origin 2>/dev/null || true; '
                    'git -C "$1" remote add origin http://127.0.0.1:9419/lifecycle.git; '
                    "git -C \"$1\" config remote.origin.fetch '+refs/heads/*:refs/remotes/origin/*'; "
                    'git -C "$1" fetch origin; '
                    'git -C "$1" branch --set-upstream-to=origin/main',
                    "workspace-lifecycle",
                    repository_ready["directory"],
                )
                repository_ready, repository_current = await expectation(
                    client, repository_workspace
                )
                assert repository_ready["git"]["upstream"] == "origin/main"
                await runtime.docker(
                    "exec",
                    runtime.name(agent_id),
                    "sh",
                    "-ceu",
                    'printf untracked > "$1/untracked.txt"',
                    "workspace-lifecycle",
                    repository_ready["directory"],
                )
                repository_changed = await client.post(
                    f"{repository_workspace}/remove", json=repository_current
                )
                assert repository_changed.status_code == 409, repository_changed.text
                repository_unsafe, _ = await expectation(client, repository_workspace)
                assert repository_unsafe["git"]["untracked"] == 1
                assert repository_unsafe["cleanup"]["remove"]["available"] is False
                await runtime.docker(
                    "exec",
                    runtime.name(agent_id),
                    "sh",
                    "-ceu",
                    "git -C \"$1\" config user.name 'Lifecycle Proof'; "
                    'git -C "$1" config user.email proof@example.invalid; '
                    'git -C "$1" add untracked.txt; git -C "$1" commit -m unpushed',
                    "workspace-lifecycle",
                    repository_ready["directory"],
                )
                unpushed, _ = await expectation(client, repository_workspace)
                assert unpushed["git"]["ahead"] == 1
                assert unpushed["cleanup"]["remove"]["available"] is False
                await runtime.docker(
                    "exec",
                    runtime.name(agent_id),
                    "git",
                    "-C",
                    repository_ready["directory"],
                    "branch",
                    "--unset-upstream",
                )
                unverifiable, _ = await expectation(client, repository_workspace)
                assert unverifiable["git"]["upstream"] is None
                assert unverifiable["cleanup"]["remove"]["available"] is False
                await runtime.docker(
                    "exec",
                    runtime.name(agent_id),
                    "sh",
                    "-ceu",
                    'git -C "$1" fetch origin; git -C "$1" reset --hard origin/main; '
                    'git -C "$1" branch -m lifecycle-retained; '
                    'git -C "$1" branch --set-upstream-to=origin/main lifecycle-retained',
                    "workspace-lifecycle",
                    repository_ready["directory"],
                )
                repository_clean, repository_clean_current = await expectation(
                    client, repository_workspace
                )
                assert repository_clean["cleanup"]["remove"]["available"] is True, repository_clean
                retained_branch = repository_clean["repository"]["working_branch"]
                retained_revision = (
                    (
                        await runtime.docker(
                            "exec",
                            runtime.name(agent_id),
                            "git",
                            "-C",
                            repository_ready["directory"],
                            "rev-parse",
                            "HEAD",
                        )
                    )
                    .decode()
                    .strip()
                )
                assert retained_branch == "lifecycle-retained"
                ready, current = await expectation(client, ordinary_workspace)
                assert ready["kind"] == "ordinary"
                assert ready["cleanup"]["remove"]["available"] is True

                # A stale receipt must never authorize deletion after real filesystem evidence changes.
                await runtime.docker(
                    "exec",
                    runtime.name(agent_id),
                    "sh",
                    "-ceu",
                    'printf dirty > "$1/change.txt"',
                    "workspace-lifecycle",
                    str(ordinary_path),
                )
                stale = await client.post(f"{ordinary_workspace}/remove", json=current)
                assert stale.status_code == 409, stale.text
                dirty, dirty_current = await expectation(client, ordinary_workspace)
                assert dirty["cleanup"]["remove"]["available"] is False
                assert dirty["git"]["state"] == "unsafe"
                refused = await client.post(f"{ordinary_workspace}/remove", json=dirty_current)
                assert refused.status_code == 409, refused.text

                # Explicit discard names the current workspace and leaves the unrelated thread alone.
                discarded = await client.post(f"{ordinary_workspace}/discard", json=dirty_current)
                assert discarded.status_code == 200, discarded.text
                assert discarded.json()["state"] == "removed"
                assert retained_path.is_dir()
                removed_read = await client.get(ordinary_workspace)
                assert removed_read.status_code == 200
                assert removed_read.json()["state"] == "removed"
                if harness == "opencode":
                    retained_history = await client.get(
                        f"{native}/{ordinary_session['id']}/message"
                    )
                    assert retained_history.status_code == 200, retained_history.text
                    assert "Workspace history proof" in retained_history.text
                else:
                    retained_history = await client.get(
                        f"{native}/{ordinary_session['id']}/history"
                    )
                    assert retained_history.status_code == 200, retained_history.text
                    assert "Workspace history proof" in retained_history.text
                assert (
                    await client.post(f"{ordinary_workspace}/remove", json=dirty_current)
                ).status_code == 409

                # Replacement is explicit, stays on the same native session/path, and increments generation.
                replacement_current = {
                    "workspace_id": removed_read.json()["workspace_id"],
                    "generation": removed_read.json()["generation"],
                    "safety_digest": removed_read.json()["safety_digest"],
                }
                replacement = await client.post(
                    f"{ordinary_workspace}/replace", json=replacement_current
                )
                assert replacement.status_code == 200, replacement.text
                replaced = replacement.json()
                assert replaced["state"] == "ready"
                assert replaced["workspace_id"] == ready["workspace_id"]
                assert replaced["generation"] == removed_read.json()["generation"] + 1
                assert replaced["directory"] == str(ordinary_path)
                assert ordinary_path.is_dir() and retained_path.is_dir()
                native_read = await client.get(f"{native}/{ordinary_session['id']}")
                assert native_read.status_code == 200, native_read.text
                assert native_read.json()["id"] == ordinary_session["id"]
                repository_removed = await client.post(
                    f"{repository_workspace}/remove", json=repository_clean_current
                )
                assert repository_removed.status_code == 200, repository_removed.text
                assert repository_removed.json()["state"] == "removed"
                grouping = await client.get(f"{base}/sessions/{repository.json()['id']}/project")
                assert grouping.status_code == 200 and grouping.json() == {
                    "project_id": project.json()["id"]
                }
                repository_replaced = await client.post(
                    f"{repository_workspace}/replace",
                    json={
                        "workspace_id": repository_removed.json()["workspace_id"],
                        "generation": repository_removed.json()["generation"],
                        "safety_digest": repository_removed.json()["safety_digest"],
                    },
                )
                assert repository_replaced.status_code == 200, repository_replaced.text
                assert repository_replaced.json()["directory"] == repository_ready["directory"]
                assert repository_replaced.json()["repository"]["working_branch"] == retained_branch
                assert (
                    await runtime.docker(
                        "exec",
                        runtime.name(agent_id),
                        "git",
                        "-C",
                        repository_ready["directory"],
                        "rev-parse",
                        "HEAD",
                    )
                ).decode().strip() == retained_revision
                repository_native = await client.get(f"{native}/{repository.json()['id']}")
                assert (
                    repository_native.status_code == 200
                    and repository_native.json()["id"] == repository.json()["id"]
                )
                await runtime.stop(org.id, agent_id)
                await runtime.start(org.id, agent_id)
                restarted, _ = await expectation(client, ordinary_workspace)
                assert restarted["directory"] == str(ordinary_path)
                history_suffix = "message" if harness == "opencode" else "history"
                history_after_restart = await client.get(
                    f"{native}/{ordinary_session['id']}/{history_suffix}"
                )
                assert history_after_restart.status_code == 200, history_after_restart.text
                assert "Workspace history proof" in history_after_restart.text

                # A harness switch freezes the old native identity; removal is still explicit,
                # while continuation through replacement remains permanently refused.
                target = "codex" if harness == "opencode" else "opencode"
                await host_app.state.host_configuration.switch_harness(org.id, agent_id, 1, target)
                await host_app.state.host_configuration.apply(
                    envelope.model_copy(
                        update={
                            "version": 2,
                            "configuration": AgentConfiguration(runtime_type=target),
                        }
                    )
                )
                frozen, frozen_current = await expectation(client, retained_workspace)
                assert frozen["frozen"] is True
                removed_frozen = await client.post(
                    f"{retained_workspace}/remove", json=frozen_current
                )
                assert removed_frozen.status_code == 200, removed_frozen.text
                frozen_replace = await client.post(
                    f"{retained_workspace}/replace", json=frozen_current
                )
                assert frozen_replace.status_code == 409, frozen_replace.text
        finally:
            await runtime.codex.transport.close()

    async def bounded():
        async with asyncio.timeout(240):
            await exercise()

    verified = False
    interrupts: list[int] = []
    try:
        _run_proof(bounded(), interrupts)
        verified = True
    finally:
        with _defer_proof_interrupts() as deferred:
            cleaned = asyncio.run(
                _dispose_proof(runtime, org.id, agent_id, directory, verified=verified)
            )
        interrupts.extend(deferred)
        if verified and not cleaned:
            raise RuntimeError("Workspace lifecycle proof fixture cleanup failed")
        if interrupts:
            raise KeyboardInterrupt("Workspace lifecycle proof interrupted")
