import asyncio
import json
import secrets
import subprocess
from uuid import uuid4

import httpx
import pytest
from httpx import ASGITransport

from fesnyng_backend.agent_host import create_app
from fesnyng_backend.host_models import HostAgentConfiguration
from fesnyng_backend.settings import ServiceSettings


@pytest.mark.parametrize(
    ("failure", "detail"),
    [
        ("connect", "Native runtime connection unavailable"),
        ("timeout", "Native runtime connection unavailable"),
        ("invalid_json", "Native runtime returned an invalid response"),
    ],
)
def test_host_messages_sanitize_native_transport_failures(tmp_path, monkeypatch, failure, detail):
    app = create_app(
        ServiceSettings(
            service="agent-host",
            database_path=tmp_path / "host.sqlite3",
            state_directory=tmp_path / "state",
        )
    )
    org, agent = str(uuid4()), str(uuid4())
    binding = secrets.token_urlsafe(32)
    store = app.state.host_store
    store.bind_organization(org, binding)
    envelope = HostAgentConfiguration(
        host_id=store.instance_id,
        organization_id=org,
        agent_id=agent,
        version=1,
        name="Engineer",
    )
    store.stage_agent(envelope)
    store.save_session(org, agent, "native-session", "default", "Thread")
    container_name = app.state.host_runtime.name(agent)
    container = json.dumps(
        {
            "labels": {
                "fesnyng.host": str(store.instance_id),
                "fesnyng.organization": org,
                "fesnyng.agent": agent,
            },
            "state": {"Running": True, "Status": "running"},
            "ports": {"4096/tcp": [{"HostIp": "127.0.0.1", "HostPort": "45678"}]},
        }
    ).encode()

    def docker_cli(command, **_):
        if command[1:3] == ["container", "ls"]:
            return subprocess.CompletedProcess(command, 0, stdout=f"{container_name}\n".encode())
        if command[1] == "inspect":
            return subprocess.CompletedProcess(command, 0, stdout=container)
        raise AssertionError(f"Unexpected Docker command: {command}")

    secret = "native-private-value"

    def native_response(request: httpx.Request) -> httpx.Response:
        if failure == "connect":
            raise httpx.ConnectError(f"connection includes {secret}", request=request)
        if failure == "timeout":
            raise httpx.ReadTimeout(f"timeout includes {secret}", request=request)
        return httpx.Response(200, content=f"invalid JSON includes {secret}".encode())

    real_async_client = httpx.AsyncClient

    def native_client(**kwargs):
        return real_async_client(transport=httpx.MockTransport(native_response), **kwargs)

    monkeypatch.setattr("fesnyng_backend.host_runtime.subprocess.run", docker_cli)
    monkeypatch.setattr("fesnyng_backend.host_runtime.httpx.AsyncClient", native_client)

    async def check():
        async with real_async_client(
            transport=ASGITransport(app),
            base_url="http://host",
            headers={"Authorization": f"Bearer {binding}"},
        ) as caller:
            response = await caller.get(
                f"/organizations/{org}/agents/{agent}/sessions/native-session/messages"
            )
            assert response.status_code == 503
            assert response.json() == {"detail": detail}
            assert secret not in response.text

    asyncio.run(check())
