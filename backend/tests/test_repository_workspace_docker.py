"""Opt-in repository preparation across real control, host, Git and native boundaries."""

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
from fesnyng_backend.control_plane import create_app
from fesnyng_backend.host_client import HostClient
from fesnyng_backend.host_models import HostAgentConfiguration
from fesnyng_backend.host_runtime import RuntimeUnavailable
from fesnyng_backend.settings import ControlPlaneSessionSettings, ServiceSettings


@pytest.mark.skipif(
    os.environ.get("FESNYNG_WORKSPACE_DOCKER_TESTS") != "true",
    reason="Requires the pinned dual-harness image and a Docker-shared host directory",
)
@pytest.mark.parametrize("harness", ["opencode", "codex"])
def test_repository_threads_are_independent_and_survive_restart(organization, monkeypatch, harness):
    settings, _control, owner, org, agents, _ = organization
    directory = Path(
        tempfile.mkdtemp(
            prefix="fesnyng-workspace-proof-", dir=os.environ.get("FESNYNG_WORKSPACE_PROOF_ROOT")
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
    agents.register_host(str(host.instance_id), "Workspace proof", "http://host.test", org.id)
    agents.set_host_credential(org.id, str(host.instance_id), token)
    agent = agents.create_agent(
        org.id,
        owner.id,
        {
            "name": "Workspace engineer",
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
        name="Workspace engineer",
        configuration=AgentConfiguration(runtime_type=harness),
    )
    monkeypatch.setattr(
        control_host_routes,
        "HostClient",
        lambda store: HostClient(store, transport=httpx.ASGITransport(host_app)),
    )
    origin = "https://workspace-proof.example"
    app = create_app(settings, ControlPlaneSessionSettings(allowed_origin=origin))
    print(f"Workspace proof: state={directory}, container={runtime.name(agent_id)}", flush=True)

    async def exercise():
        try:
            await host_app.state.host_configuration.apply(envelope)
            legacy_directory = "/workspace/default/threads/legacy-proof"
            legacy = await runtime.create_session(
                org.id, agent_id, "Retained legacy thread", "default", directory=legacy_directory
            )
            await runtime.docker(
                "exec",
                runtime.name(agent_id),
                "sh",
                "-ceu",
                'printf retained > "$1/legacy.txt"',
                "legacy-proof",
                legacy_directory,
            )
            # A loopback-only dumb HTTP Git origin lives inside this exact proof container.
            # It needs neither provider credentials nor another exposed service/container.
            await runtime.docker(
                "exec",
                runtime.name(agent_id),
                "sh",
                "-ceu",
                "mkdir -p /home/agent/proof-remotes; "
                "for repo in alpha beta; do "
                "git init -b main /home/agent/source-$repo; "
                "git -C /home/agent/source-$repo config user.name 'Proof Engineer'; "
                "git -C /home/agent/source-$repo config user.email proof@example.invalid; "
                "printf '%s-main' \"$repo\" > /home/agent/source-$repo/origin.txt; "
                "git -C /home/agent/source-$repo add origin.txt; "
                "git -C /home/agent/source-$repo commit -m main; "
                "git -C /home/agent/source-$repo checkout -b test; "
                "printf '%s-test' \"$repo\" > /home/agent/source-$repo/origin.txt; "
                "git -C /home/agent/source-$repo commit -am test; "
                "git clone --bare /home/agent/source-$repo /home/agent/proof-remotes/$repo.git; "
                "git --git-dir=/home/agent/proof-remotes/$repo.git update-server-info; done",
            )
            await runtime.docker(
                "exec",
                "-d",
                runtime.name(agent_id),
                "node",
                "-e",
                "const http=require('node:http'),fs=require('node:fs'),path=require('node:path');"
                "http.createServer((req,res)=>{"
                "const p=new URL(req.url,'http://localhost').pathname;"
                "if(p.startsWith('/private.git')){res.writeHead(401);res.end();return;}"
                "const f=path.resolve('/home/agent/proof-remotes','.'+p);"
                "if(!f.startsWith('/home/agent/proof-remotes/')){res.writeHead(404);res.end();return;}"
                "fs.readFile(f,(e,b)=>{res.writeHead(e?404:200);res.end(e?'missing':b);});"
                "}).listen(9418,'127.0.0.1');",
            )
            await runtime.docker(
                "exec",
                runtime.name(agent_id),
                "node",
                "-e",
                "fetch('http://127.0.0.1:9418/alpha.git/HEAD').then(r=>{if(!r.ok)process.exit(1)})",
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
                projects = f"/organizations/{org.id}/projects"
                base = f"/organizations/{org.id}/agents/{agent_id}"
                native = f"{base}/{harness}/session"
                project_ids = []
                for name in ("alpha", "beta", "private"):
                    result = await client.post(
                        projects,
                        json={
                            "name": name,
                            "target_repository_url": f"http://127.0.0.1:9418/{name}.git",
                            "default_checkout_branch": "test",
                        },
                    )
                    assert result.status_code == 201, result.text
                    project_ids.append(result.json()["id"])
                requests = [
                    {
                        "title": "Alpha test",
                        "project_id": project_ids[0],
                        "creation_id": str(uuid4()),
                    },
                    {
                        "title": "Alpha main",
                        "project_id": project_ids[0],
                        "checkout_branch": "main",
                        "creation_id": str(uuid4()),
                    },
                    {
                        "title": "Beta test",
                        "project_id": project_ids[1],
                        "creation_id": str(uuid4()),
                    },
                ]
                responses = await asyncio.gather(
                    *(client.post(native, json=body) for body in requests)
                )
                assert all(reply.status_code == 201 for reply in responses), [
                    r.text for r in responses
                ]
                threads = [reply.json() for reply in responses]
                paths = [Path(thread["directory"]) for thread in threads]
                assert len(set(paths)) == 3
                assert all(path.is_relative_to(shared / org.id / agent_id) for path in paths)
                assert [p.joinpath("origin.txt").read_text() for p in paths] == [
                    "alpha-test",
                    "alpha-main",
                    "beta-test",
                ]
                branches = []
                for index, path in enumerate(paths):
                    path.joinpath("independent.txt").write_text(str(index))
                    branches.append(
                        (
                            await runtime.docker(
                                "exec",
                                runtime.name(agent_id),
                                "git",
                                "-C",
                                str(path),
                                "branch",
                                "--show-current",
                            )
                        )
                        .decode()
                        .strip()
                    )
                    context = await runtime.workspace_context(org.id, agent_id, str(path))
                    assert context["repository"]["state"] == "available"
                    assert context["branch"]["state"] == "available"
                assert len(set(branches)) == 3
                repeated = await client.post(native, json=requests[0])
                assert repeated.status_code == 201, repeated.text
                assert repeated.json()["id"] == threads[0]["id"]
                ordinary = await client.post(
                    native, json={"title": "Ordinary", "creation_id": str(uuid4())}
                )
                assert ordinary.status_code == 201, ordinary.text
                ordinary_path = Path(ordinary.json()["directory"])
                assert ordinary_path.is_relative_to(shared / org.id / agent_id)
                assert ordinary_path.is_dir() and not (ordinary_path / ".git").exists()
                # Lose one real native creation response before the host can save its mapping.
                # Recovery must discover that exact native root, not create a replacement.
                recovery_request = {
                    "title": "Recover interrupted preparation",
                    "project_id": project_ids[0],
                    "creation_id": str(uuid4()),
                }
                native_created = []
                discovery_available = False
                with monkeypatch.context() as failure:
                    if harness == "codex":
                        original_call = runtime.codex.transport.call

                        async def lose_codex_receipt(organization_id, employee_id, method, params):
                            if (
                                native_created
                                and not discovery_available
                                and method in {"thread/list", "thread/loaded/list", "thread/read"}
                            ):
                                raise RuntimeUnavailable("Simulated temporary discovery outage")
                            result = await original_call(
                                organization_id, employee_id, method, params
                            )
                            if method == "thread/start":
                                native_created.append(result["thread"]["id"])
                                raise RuntimeUnavailable("Simulated lost native creation response")
                            return result

                        failure.setattr(runtime.codex.transport, "call", lose_codex_receipt)
                    else:
                        original_request = runtime.request

                        async def lose_opencode_receipt(
                            organization_id, employee_id, path, **kwargs
                        ):
                            if (
                                native_created
                                and not discovery_available
                                and path == "/session"
                                and kwargs.get("method", "GET") == "GET"
                            ):
                                raise RuntimeUnavailable("Simulated temporary discovery outage")
                            result = await original_request(
                                organization_id, employee_id, path, **kwargs
                            )
                            if path == "/session" and kwargs.get("method") == "POST":
                                native_created.append(result["id"])
                                raise RuntimeUnavailable("Simulated lost native creation response")
                            return result

                        failure.setattr(runtime, "request", lose_opencode_receipt)
                    lost = await client.post(native, json=recovery_request)
                    assert lost.status_code == 503, lost.text
                    changed_default = await client.patch(
                        f"{projects}/{project_ids[0]}",
                        json={"default_checkout_branch": "main"},
                    )
                    assert changed_default.status_code == 200, changed_default.text
                    discovery_available = True
                    recovered = await client.post(native, json=recovery_request)
                    assert recovered.status_code == 201, recovered.text
                assert len(native_created) == 1
                reservation = host.workspace_creation(
                    org.id, agent_id, recovery_request["creation_id"]
                )
                assert (
                    Path(reservation["directory"]).joinpath("origin.txt").read_text()
                    == "alpha-test"
                )
                assert recovered.json()["id"] == native_created[0]
                recovered_read = await client.get(f"{native}/{native_created[0]}")
                assert recovered_read.status_code == 200, recovered_read.text
                recovered_threads = [recovered.json()]
                if harness == "codex":
                    injection_request = {
                        "title": "Recover initialized workspace",
                        "project_id": project_ids[0],
                        "creation_id": str(uuid4()),
                    }
                    initialized_ids = []
                    injected_items = []
                    injection_discovery_available = False
                    original_call = runtime.codex.transport.call

                    async def lose_initialization_receipt(org_id, employee_id, method, params):
                        if (
                            injected_items
                            and not injection_discovery_available
                            and method
                            in {"thread/list", "thread/loaded/list", "thread/read", "thread/resume"}
                        ):
                            raise RuntimeUnavailable("Simulated temporary discovery outage")
                        result = await original_call(org_id, employee_id, method, params)
                        if method == "thread/start":
                            initialized_ids.append(result["thread"]["id"])
                        if method == "thread/inject_items":
                            injected_items.append(params["items"])
                            raise RuntimeUnavailable("Simulated lost initialization response")
                        return result

                    with monkeypatch.context() as failure:
                        failure.setattr(
                            runtime.codex.transport, "call", lose_initialization_receipt
                        )
                        lost_initialization = await client.post(native, json=injection_request)
                        assert lost_initialization.status_code == 503, lost_initialization.text
                        injection_discovery_available = True
                        initialized = await client.post(native, json=injection_request)
                        assert initialized.status_code == 201, initialized.text
                    assert initialized_ids == [initialized.json()["id"]]
                    assert len(injected_items) == 1
                    assert len(injected_items[0]) == 1
                    assert injected_items[0][0]["role"] == "developer"
                    recovered_threads.append(initialized.json())
                    for item in recovered_threads:
                        history = await runtime.codex.call(
                            org.id,
                            agent_id,
                            "thread/read",
                            {"threadId": item["id"], "includeTurns": True},
                        )
                        assert history["thread"]["turns"] == []
                for body, expected_failure in (
                    ({"project_id": project_ids[0], "checkout_branch": "does-not-exist"}, "branch"),
                    ({"project_id": project_ids[2]}, "authentication"),
                ):
                    failed = await client.post(native, json={**body, "creation_id": str(uuid4())})
                    assert failed.status_code >= 400, failed.text
                    assert expected_failure in failed.text.lower(), failed.text
                    assert "uncertain" not in failed.text.lower(), failed.text
                inventory = await client.get(native)
                assert len(inventory.json()) == 5 + len(recovered_threads), inventory.text
                if harness == "opencode":
                    await runtime.docker(
                        "exec", runtime.name(agent_id), "rm", str(paths[0] / "origin.txt")
                    )
                    await runtime.docker(
                        "exec",
                        runtime.name(agent_id),
                        "test",
                        "!",
                        "-e",
                        str(paths[0] / "origin.txt"),
                    )
                    deleted = await runtime.docker(
                        "exec",
                        runtime.name(agent_id),
                        "git",
                        "-C",
                        str(paths[0]),
                        "diff",
                        "--name-only",
                        "--diff-filter=D",
                        "HEAD",
                    )
                    assert "origin.txt" in deleted.decode().splitlines()
                    forked = await client.post(f"{native}/{threads[0]['id']}/fork", json={})
                    assert forked.status_code == 201, forked.text
                    fork_path = Path(forked.json()["directory"])
                    assert fork_path != paths[0]
                    await runtime.docker(
                        "exec",
                        runtime.name(agent_id),
                        "test",
                        "!",
                        "-e",
                        str(fork_path / "origin.txt"),
                    )
                    assert not fork_path.joinpath("origin.txt").exists()
                    assert fork_path.joinpath("independent.txt").read_text() == "0"
                    fork_path.joinpath("independent.txt").write_text("fork")
                    assert paths[0].joinpath("independent.txt").read_text() == "0"
                await runtime.codex.transport.close()
                await runtime.stop(org.id, agent_id)
                await runtime.start(org.id, agent_id)
                legacy_receipt = await client.get(f"{native}/{legacy['id']}")
                assert legacy_receipt.status_code == 200, legacy_receipt.text
                assert legacy_receipt.json()["directory"] == legacy_directory
                assert (
                    await runtime.docker(
                        "exec", runtime.name(agent_id), "cat", f"{legacy_directory}/legacy.txt"
                    )
                    == b"retained"
                )
                for index, path in enumerate(paths):
                    assert path.joinpath("independent.txt").read_text() == str(index)
                    receipt = await client.get(f"{native}/{threads[index]['id']}")
                    assert receipt.status_code == 200, receipt.text
                    assert receipt.json()["directory"] == str(path)
                for item in recovered_threads:
                    receipt = await client.get(f"{native}/{item['id']}")
                    assert receipt.status_code == 200, receipt.text
                    assert receipt.json()["directory"] == item["directory"]
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
            raise RuntimeError("Workspace proof fixture cleanup failed")
        if interrupts:
            raise KeyboardInterrupt("Workspace proof interrupted")
