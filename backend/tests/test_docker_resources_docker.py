"""Opt-in proof that an allowed employee can register a shared Compose resource."""

import asyncio
import json
import os
import secrets
import socket
import tempfile
from pathlib import Path
from uuid import UUID, uuid4

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
from fesnyng_backend.settings import (
    ControlPlaneSessionSettings,
    DockerCapabilitySettings,
    ServiceSettings,
    TailscaleServeSettings,
)


@pytest.mark.skipif(
    os.environ.get("FESNYNG_DOCKER_RESOURCES_DOCKER_TESTS") != "true",
    reason="Requires an organization-dedicated Docker engine and docker-121 runtime image",
)
def test_employee_compose_resource_is_registered_without_owning_its_thread_lifetime(
    organization, monkeypatch
):
    """The public control route manages one employee-created sibling by immutable ID."""
    settings, _control, owner, org, agents, host_id = organization
    root = Path(
        tempfile.mkdtemp(
            prefix="fesnyng-docker-resources-proof-",
            dir=os.environ.get("FESNYNG_DOCKER_RESOURCES_PROOF_ROOT"),
        )
    ).resolve()
    workspace_root = root / "workspaces"
    employee = agents.create_agent(
        org.id,
        owner.id,
        {
            "name": "Docker engineer",
            "host_id": host_id,
            "configuration": {"runtime_type": "opencode"},
        },
    )
    employee_id = str(employee["id"])
    engine_id = os.environ.get("FESNYNG_DOCKER_RESOURCES_ENGINE_ID")
    socket_path = os.environ.get("FESNYNG_DOCKER_RESOURCES_SOCKET_PATH")
    if not engine_id or not socket_path:
        pytest.fail("Proof requires explicit dedicated Docker engine ID and socket path")
    tailscale_proof = os.environ.get("FESNYNG_SERVICES_TAILSCALE_PROOF") == "true"
    host_app = create_host(
        ServiceSettings(
            service="agent-host",
            instance_id=UUID(host_id),
            state_directory=root,
            database_path=root / "host.sqlite3",
            workspace_root=workspace_root,
            docker_capability=DockerCapabilitySettings(
                organization_id=UUID(org.id),
                engine_id=engine_id,
                socket_path=Path(socket_path),
                employee_ids=(UUID(employee_id),),
            ),
            tailscale_serve=(
                TailscaleServeSettings(
                    organization_id=UUID(org.id),
                    command=("sudo", "-n", "tailscale", "serve", "status", "--json"),
                )
                if tailscale_proof
                else None
            ),
        )
    )
    host, runtime = host_app.state.host_store, host_app.state.host_runtime
    token = secrets.token_urlsafe(32)
    host.bind_organization(org.id, token)
    agents.register_host(host_id, "Docker resource proof", "http://host.test", org.id)
    agents.set_host_credential(org.id, host_id, token)
    envelope = HostAgentConfiguration(
        host_id=host.instance_id,
        organization_id=org.id,
        agent_id=employee_id,
        version=1,
        name="Docker engineer",
        configuration=AgentConfiguration(runtime_type="opencode"),
    )
    monkeypatch.setattr(
        control_host_routes,
        "HostClient",
        lambda store: HostClient(store, transport=httpx.ASGITransport(host_app)),
    )
    origin = "https://docker-resources-proof.example"
    app = create_app(settings, ControlPlaneSessionSettings(allowed_origin=origin))
    proof_id = uuid4().hex
    compose_project = f"fesnyng121{proof_id[:12]}"
    sibling_name = f"fesnyng121-sibling-{proof_id[:12]}"
    proxy_name = f"fesnyng122-proxy-{proof_id[:12]}"
    serve_port = 18443
    proxy_port = 18081
    serve_created = False
    proxy_created = False

    async def tailscale(*args: str) -> bytes:
        process = await asyncio.create_subprocess_exec(
            "sudo",
            "-n",
            "tailscale",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=20)
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        if process.returncode:
            raise RuntimeError("Task-owned Tailscale proof operation failed")
        return stdout

    print(
        "Docker resources proof: "
        f"state={root}, employee={runtime.name(employee_id)}, "
        f"compose_project={compose_project}, proof_label={proof_id}",
        flush=True,
    )

    async def employee_compose(directory: str, action: str) -> bytes:
        return await runtime.docker(
            "exec",
            runtime.name(employee_id),
            "sh",
            "-ceu",
            f'docker compose -p "{compose_project}" -f "$1/compose.yaml" {action}',
            "docker-resources-proof",
            directory,
        )

    async def stop_or_remove_sibling(directory: str, *, verified: bool) -> None:
        try:
            await employee_compose(
                directory,
                "down --volumes --remove-orphans" if verified else "stop",
            )
        except RuntimeUnavailable:
            # The employee teardown below reports its retained diagnostic identity.
            pass

    async def exercise() -> None:
        nonlocal workspace, serve_created, proxy_created
        await host_app.state.host_configuration.apply(envelope)
        assert await runtime.docker_engine_id(Path(socket_path)) == engine_id
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url=origin, headers={"Origin": origin}
        ) as client:
            login = await client.post(
                "/auth/login", json={"login": "owner", "password": "correct horse battery staple"}
            )
            assert login.status_code == 200, login.text
            client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
            native = f"/organizations/{org.id}/agents/{employee_id}/opencode/session"
            first = await client.post(native, json={"title": "Compose service owner"})
            second = await client.post(native, json={"title": "Compose service observer"})
            assert first.status_code == second.status_code == 201
            first_session, second_session = first.json(), second.json()
            workspace = Path(first_session["directory"])
            compose = f'''services:
  app:
    image: fesnyng-agent:docker-121
    container_name: {sibling_name}
    entrypoint: ["node", "-e", "require('http').createServer((_, res) => res.end(require('fs').readFileSync('/work/proof.txt'))).listen(8080)"]
    labels:
      fesnyng.proof: "{proof_id}"
    volumes:
      - .:/work:ro
'''.encode()
            await runtime.docker(
                "exec",
                "-i",
                runtime.name(employee_id),
                "sh",
                "-ceu",
                'cat > "$1/compose.yaml"',
                "docker-resources-proof",
                str(workspace),
                content=compose,
            )
            await runtime.docker(
                "exec",
                runtime.name(employee_id),
                "sh",
                "-ceu",
                'printf %s "shared compose evidence" > "$1/proof.txt"',
                "docker-resources-proof",
                str(workspace),
            )
            await employee_compose(str(workspace), "up -d")
            sibling_id = (await employee_compose(str(workspace), "ps -q app")).decode().strip()
            assert len(sibling_id) == 64 and sibling_id.islower()
            assert (
                await runtime.docker("exec", sibling_id, "cat", "/work/proof.txt")
            ) == b"shared compose evidence"

            resources = f"/organizations/{org.id}/hosts/{host_id}/docker"
            registered = await client.post(
                f"{resources}/resources",
                json={
                    "container_id": sibling_id,
                    "name": "Proof shared Compose app",
                    "threads": [
                        {"agent_id": employee_id, "session_id": first_session["id"]},
                        {"agent_id": employee_id, "session_id": second_session["id"]},
                    ],
                    "project_ids": [],
                },
            )
            assert registered.status_code == 200, registered.text
            resource = registered.json()
            assert resource["container_id"] == sibling_id
            assert resource["inspection"]["state"] == "running"
            assert len(resource["threads"]) == 2
            assert resource["inspection"]["compose_project"] == compose_project
            assert resource["inspection"]["mounts"] == [
                {
                    "type": "bind",
                    "source": str(workspace),
                    "destination": "/work",
                    "read_only": True,
                }
            ]
            inventory = await client.get(resources)
            assert inventory.status_code == 200, inventory.text
            assert inventory.json()["capability"] == {
                "enabled": True,
                "available": True,
                "reason": None,
                "engine_id": engine_id,
            }
            assert [item["id"] for item in inventory.json()["resources"]] == [resource["id"]]

            # Service registration uses the same real host ownership boundary for
            # both an employee-internal application and its Compose sibling.
            await runtime.docker(
                "exec",
                "-d",
                runtime.name(employee_id),
                "node",
                "-e",
                "require('http').createServer((_,r)=>r.end('employee application')).listen(8081)",
            )
            services = f"{resources}/services"
            registered_services = []
            application_addresses = {}
            for kind, target_id, container, port, expected in (
                ("employee", employee_id, runtime.name(employee_id), 8081, "employee application"),
                ("resource", resource["id"], sibling_id, 8080, "shared compose evidence"),
            ):
                address = (
                    (
                        await runtime.docker(
                            "inspect",
                            "--format",
                            "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                            container,
                        )
                    )
                    .decode()
                    .strip()
                )
                endpoint = f"http://{address}:{port}"
                application_addresses[kind] = {"host": address, "port": port}
                async with httpx.AsyncClient(trust_env=False) as application:
                    for attempt in range(20):
                        try:
                            response = await application.get(endpoint, timeout=2)
                            assert response.text == expected
                            break
                        except httpx.TransportError:
                            if attempt == 19:
                                raise
                            await asyncio.sleep(0.1)
                reply = await client.post(
                    services,
                    json={
                        "name": f"Proof {kind} application",
                        "target_kind": kind,
                        "target_id": target_id,
                        "endpoint_url": endpoint,
                        "route": "custom",
                        "threads": resource["threads"],
                        "project_ids": [],
                    },
                )
                assert reply.status_code == 200, reply.text
                service = reply.json()
                assert service["target"]["running"] is True
                assert service["route_status"]["network_reachability"] == "unverified"
                assert len(service["threads"]) == 2
                registered_services.append(service)
            listed = await client.get(services)
            assert listed.status_code == 200, listed.text
            assert {s["id"] for s in listed.json()["services"]} == {
                s["id"] for s in registered_services
            }

            if tailscale_proof:
                # Explicit opt-in on the private organization proof host. Refuse
                # pre-existing Serve configuration rather than replacing it.
                prior = json.loads(await tailscale("serve", "status", "--json"))
                if prior:
                    pytest.fail("Tailscale proof requires no existing Serve configuration")
                for port in (proxy_port, serve_port):
                    with socket.socket() as reserved:
                        reserved.bind(("0.0.0.0", port))
                self_status = json.loads(await tailscale("status", "--json"))
                dns_name = self_status["Self"]["DNSName"].rstrip(".")
                proxy_script = (
                    "const http=require('http');const targets=JSON.parse(process.argv[1]);"
                    "http.createServer((q,r)=>{const t=targets[q.url.slice(1)];"
                    "if(!t){r.writeHead(404);r.end();return;}"
                    "const p=http.get({host:t.host,port:t.port,path:'/'},s=>{"
                    "r.writeHead(s.statusCode);s.pipe(r);});"
                    "p.on('error',()=>{r.writeHead(502);r.end();});"
                    f"}}).listen({proxy_port},'127.0.0.1');"
                )
                await runtime.docker(
                    "run",
                    "-d",
                    "--name",
                    proxy_name,
                    "--network",
                    "host",
                    "--label",
                    f"fesnyng.proof={proof_id}",
                    "--entrypoint",
                    "node",
                    "fesnyng-agent:docker-121",
                    "-e",
                    proxy_script,
                    json.dumps(application_addresses),
                )
                proxy_created = True
                async with httpx.AsyncClient(trust_env=False) as application:
                    for attempt in range(20):
                        try:
                            local = await application.get(f"http://127.0.0.1:{proxy_port}/employee")
                            assert local.text == "employee application"
                            break
                        except httpx.TransportError:
                            if attempt == 19:
                                raise
                            await asyncio.sleep(0.1)
                # Mark the exact route before the call so an uncertain reply is
                # still followed by its narrow teardown, never a global reset.
                serve_created = True
                await tailscale(
                    "serve",
                    "--yes",
                    "--bg",
                    f"--https={serve_port}",
                    f"http://127.0.0.1:{proxy_port}",
                )
                for index, kind in enumerate(("employee", "resource")):
                    service = registered_services[index]
                    endpoint = f"https://{dns_name}:{serve_port}/{kind}"
                    updated = await client.put(
                        f"{services}/{service['id']}",
                        json={
                            "name": service["name"],
                            "target_kind": kind,
                            "target_id": service["target"]["id"],
                            "endpoint_url": endpoint,
                            "route": "tailscale",
                            "threads": service["threads"],
                            "project_ids": [],
                            "expected_revision": service["revision"],
                        },
                    )
                    assert updated.status_code == 200, updated.text
                    registered_services[index] = updated.json()
                    assert updated.json()["route_status"]["status"] == "configured"
                    assert updated.json()["route_status"]["network_reachability"] == "unverified"
                    async with httpx.AsyncClient(trust_env=False) as application:
                        # Serve configuration is not a listener-readiness receipt.
                        # The initial live route can still be establishing its
                        # connection path; require an actual response within a
                        # bounded startup window, never weaken response checks.
                        for attempt in range(12):
                            try:
                                result = await application.get(endpoint, timeout=5)
                                assert result.status_code == 200
                                assert result.text == (
                                    "employee application"
                                    if kind == "employee"
                                    else "shared compose evidence"
                                )
                                break
                            except httpx.TransportError:
                                if attempt == 11:
                                    raise
                                await asyncio.sleep(0.25)

            # A native archive never owns the external service's lifecycle.
            archived = await client.patch(
                f"{native}/{first_session['id']}", json={"time": {"archived": 1_789_268_000_000}}
            )
            assert archived.status_code == 200, archived.text
            retained = await client.get(f"{resources}/resources/{resource['id']}")
            assert retained.status_code == 200, retained.text
            assert retained.json()["inspection"]["state"] == "running"

            expectation = {"expected_revision": resource["revision"]}
            stopped = await client.post(
                f"{resources}/resources/{resource['id']}/stop", json=expectation
            )
            assert stopped.status_code == 200, stopped.text
            assert stopped.json()["inspection"]["state"] == "exited"
            stopped_service = await client.get(f"{services}/{registered_services[1]['id']}")
            assert stopped_service.status_code == 200, stopped_service.text
            assert stopped_service.json()["target"]["running"] is False
            started = await client.post(
                f"{resources}/resources/{resource['id']}/start", json=expectation
            )
            assert started.status_code == 200, started.text
            assert started.json()["inspection"]["state"] == "running"
            stopped_again = await client.post(
                f"{resources}/resources/{resource['id']}/stop", json=expectation
            )
            assert stopped_again.status_code == 200, stopped_again.text
            removed = await client.post(
                f"{resources}/resources/{resource['id']}/remove", json=expectation
            )
            assert removed.status_code == 200, removed.text
            assert removed.json()["inspection"]["status"] == "missing"
            missing_service = await client.get(f"{services}/{registered_services[1]['id']}")
            assert missing_service.status_code == 200, missing_service.text
            assert missing_service.json()["target"]["status"] == "missing"
            for service in registered_services:
                unregistered = await client.post(
                    f"{services}/{service['id']}/unregister",
                    json={"expected_revision": service["revision"]},
                )
                assert unregistered.status_code == 200, unregistered.text
            assert (await runtime.inspect(org.id, employee_id))["state"]["Running"]
            # The managed sibling is gone, then Compose releases its exact network while
            # its project file still exists. Do not hide a failed namespace cleanup.
            await employee_compose(str(workspace), "down --volumes --remove-orphans")
            assert (
                not (
                    await runtime.docker(
                        "network",
                        "ls",
                        "--filter",
                        f"label=com.docker.compose.project={compose_project}",
                        "--format",
                        "{{.Name}}",
                    )
                )
                .decode()
                .strip()
            )
            # Native session creation owns root-created children across both proof threads.
            # Empty the exact employee bind through its owner; the bind mount itself
            # cannot be removed from inside the employee.
            await runtime.docker(
                "exec",
                runtime.name(employee_id),
                "sh",
                "-ceu",
                'find "$1" -mindepth 1 -depth -delete',
                "docker-resources-proof",
                str(runtime.employee_workspace_root(org.id, employee_id)),
            )
            assert not any(runtime.employee_workspace_root(org.id, employee_id).iterdir())

    async def bounded() -> None:
        async with asyncio.timeout(240):
            await exercise()

    verified = False
    interrupts: list[int] = []
    workspace: Path | None = None
    try:
        _run_proof(bounded(), interrupts)
        verified = True
    finally:
        route_cleanup_errors = []
        if serve_created:
            try:
                asyncio.run(tailscale("serve", "--yes", f"--https={serve_port}", "off"))
            except (RuntimeError, OSError, TimeoutError) as error:
                route_cleanup_errors.append(error)
        if proxy_created:
            try:
                asyncio.run(runtime.docker("stop", proxy_name))
                if verified:
                    asyncio.run(runtime.docker("rm", proxy_name))
            except RuntimeUnavailable as error:
                route_cleanup_errors.append(error)
        # The success path releases its Compose namespace before deleting its project file.
        # On failure, stop exact compute but retain the state and Compose diagnostics.
        if workspace is not None and not verified:
            asyncio.run(stop_or_remove_sibling(str(workspace), verified=False))
        with _defer_proof_interrupts() as deferred:
            cleaned = asyncio.run(
                _dispose_proof(runtime, org.id, employee_id, root, verified=verified)
            )
        interrupts.extend(deferred)
        if verified and not cleaned:
            raise RuntimeError("Docker resource proof fixture cleanup failed")
        if route_cleanup_errors:
            raise RuntimeError("Service route proof cleanup failed; inspect exact proof resources")
        if interrupts:
            raise KeyboardInterrupt("Docker resource proof interrupted")
