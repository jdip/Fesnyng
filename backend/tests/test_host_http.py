import asyncio
import secrets
from uuid import uuid4

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from fesnyng_backend.agent_host import create_app
from fesnyng_backend.settings import ServiceSettings


def test_host_requires_organization_binding_and_never_uses_human_cookie(tmp_path):
    app = create_app(
        ServiceSettings(
            service="agent-host",
            database_path=tmp_path / "host.sqlite3",
            state_directory=tmp_path / "state",
        )
    )
    org, other = str(uuid4()), str(uuid4())
    token = secrets.token_urlsafe(32)
    app.state.host_store.bind_organization(org, token)

    async def check():
        async with AsyncClient(transport=ASGITransport(app), base_url="http://host") as client:
            path = f"/organizations/{other}/agents/{uuid4()}"
            assert (await client.get(path)).status_code == 401
            client.cookies.set("fesnyng_session", token)
            assert (await client.get(path)).status_code == 401
            client.headers["Authorization"] = f"Bearer {token}"
            assert (await client.get(path)).status_code == 403
            assert (await client.get(f"/organizations/{org}/agents/{uuid4()}")).status_code == 404

    asyncio.run(check())


def test_profile_management_and_agent_credentials_have_separate_authority(tmp_path):
    import base64
    import json

    import httpx

    from fesnyng_backend.host_credentials import CredentialService

    app = create_app(
        ServiceSettings(
            service="agent-host",
            database_path=tmp_path / "host.sqlite3",
            state_directory=tmp_path / "state",
        )
    )
    org, other, profile, aid = (str(uuid4()) for _ in range(4))
    binding, agent_key = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    app.state.host_store.bind_organization(org, binding)

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(500))
        ) as provider:
            app.state.credential_service = CredentialService(app.state.credential_store, provider)
            async with AsyncClient(
                transport=ASGITransport(app),
                base_url="http://host",
                headers={"Authorization": f"Bearer {binding}"},
            ) as client:
                assert (
                    await client.put(
                        f"/organizations/{other}/profiles/{profile}", json={"name": "Foreign"}
                    )
                ).status_code == 403
                assert (
                    await client.put(
                        f"/organizations/{org}/profiles/{profile}", json={"name": "Shared"}
                    )
                ).status_code == 200
                store = app.state.credential_store
                store.assign_agent(org, aid, profile, agent_key)
                operation = store.acquire_operation(
                    org, profile, ["login_required"], "login_pending"
                )
                payload = (
                    base64.urlsafe_b64encode(
                        json.dumps({"chatgpt_account_id": "example-account"}).encode()
                    )
                    .decode()
                    .rstrip("=")
                )
                store.save_tokens(
                    org,
                    profile,
                    {
                        "access_token": f"example.{payload}.signature",
                        "refresh_token": "example-refresh",
                        "expires_in": 3600,
                    },
                    operation,
                )
                assert (await client.get("/credential")).status_code == 403
                client.headers["Authorization"] = f"Bearer {agent_key}"
                assert (
                    await client.get(f"/organizations/{org}/profiles/{profile}")
                ).status_code == 401
                access = await client.get("/credential")
                assert access.status_code == 200 and access.headers["cache-control"] == "no-store"
                assert "refresh" not in access.json() and access.json()["profile_id"] == profile
                store.unassign_agent(org, aid)
                assert (await client.get("/credential")).status_code == 403

    asyncio.run(check())


def test_host_completes_device_authorization_without_control_plane_polling(tmp_path):
    import base64
    import json

    import httpx

    app = create_app(
        ServiceSettings(
            service="agent-host",
            database_path=tmp_path / "host.sqlite3",
            state_directory=tmp_path / "state",
        )
    )
    org, profile = str(uuid4()), str(uuid4())
    binding = secrets.token_urlsafe(32)
    app.state.host_store.bind_organization(org, binding)
    app.state.credential_store.ensure_profile(org, profile, "Shared")
    payload = (
        base64.urlsafe_b64encode(json.dumps({"chatgpt_account_id": "example-account"}).encode())
        .decode()
        .rstrip("=")
    )
    paths = []

    def provider(request):
        paths.append(request.url.path)
        if request.url.path.endswith("/usercode"):
            return httpx.Response(
                200,
                json={"user_code": "EXAMPLE", "device_auth_id": "example-device", "interval": 1},
            )
        if request.url.path.endswith("/deviceauth/token"):
            return httpx.Response(
                200,
                json={"authorization_code": "example-code", "code_verifier": "example-verifier"},
            )
        return httpx.Response(
            200,
            json={
                "access_token": f"example.{payload}.signature",
                "refresh_token": "example-refresh",
                "expires_in": 3600,
            },
        )

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            app.state.provider_client = client
            async with AsyncClient(
                transport=ASGITransport(app),
                base_url="http://host",
                headers={"Authorization": f"Bearer {binding}"},
            ) as caller:
                response = await caller.post(f"/organizations/{org}/profiles/{profile}/login")
                assert response.status_code == 200
                assert response.json()["verification_uri"] == "https://auth.openai.com/codex/device"
                await asyncio.gather(*list(app.state.login_tasks))
                status = (await caller.get(f"/organizations/{org}/profiles/{profile}")).json()
                assert status["state"] == "ready" and status["generation"] == 1
                assert "refresh" not in status and "access" not in status

    asyncio.run(check())
    assert paths == [
        "/api/accounts/deviceauth/usercode",
        "/api/accounts/deviceauth/token",
        "/oauth/token",
    ]


@pytest.mark.parametrize("invalid_device", [None, []])
def test_host_login_releases_operation_after_invalid_device_response(tmp_path, invalid_device):
    app = create_app(
        ServiceSettings(
            service="agent-host",
            database_path=tmp_path / "host.sqlite3",
            state_directory=tmp_path / "state",
        )
    )
    org, profile = str(uuid4()), str(uuid4())
    binding = secrets.token_urlsafe(32)
    app.state.host_store.bind_organization(org, binding)
    app.state.credential_store.ensure_profile(org, profile, "Shared")

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=invalid_device))
        ) as provider:
            app.state.provider_client = provider
            async with AsyncClient(
                transport=ASGITransport(app),
                base_url="http://host",
                headers={"Authorization": f"Bearer {binding}"},
            ) as caller:
                path = f"/organizations/{org}/profiles/{profile}/login"
                assert (await caller.post(path)).status_code == 503
                assert (await caller.post(path)).status_code == 503
                status = (await caller.get(f"/organizations/{org}/profiles/{profile}")).json()
                assert status["state"] == "login_required"
                assert not app.state.login_tasks

    asyncio.run(check())


def test_host_login_releases_operation_when_initial_provider_call_is_cancelled(tmp_path):
    app = create_app(
        ServiceSettings(
            service="agent-host",
            database_path=tmp_path / "host.sqlite3",
            state_directory=tmp_path / "state",
        )
    )
    org, profile = str(uuid4()), str(uuid4())
    binding = secrets.token_urlsafe(32)
    app.state.host_store.bind_organization(org, binding)
    app.state.credential_store.ensure_profile(org, profile, "Shared")

    def cancel_provider(_: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(cancel_provider)) as provider:
            app.state.provider_client = provider
            async with AsyncClient(
                transport=ASGITransport(app),
                base_url="http://host",
                headers={"Authorization": f"Bearer {binding}"},
            ) as caller:
                path = f"/organizations/{org}/profiles/{profile}/login"
                with pytest.raises(asyncio.CancelledError):
                    await caller.post(path)
                status = (await caller.get(f"/organizations/{org}/profiles/{profile}")).json()
                assert status["state"] == "login_required"
                assert not app.state.login_tasks

    asyncio.run(check())
