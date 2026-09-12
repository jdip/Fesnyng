import asyncio
from pathlib import Path

import httpx

from fesnyng_backend.control_plane import create_app
from fesnyng_backend.settings import ControlPlaneSessionSettings, ServiceSettings


def run(app, action):
    async def request():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await action(client)

    return asyncio.run(request())


def test_user_can_log_in_and_read_their_identity(tmp_path: Path):
    app = create_app(
        ServiceSettings(
            service="control-plane",
            database_path=tmp_path / "control-plane.sqlite3",
            state_directory=tmp_path / "state",
        ),
        session_settings=ControlPlaneSessionSettings(
            cookie_secure=False, allowed_origin="http://test"
        ),
    )
    app.state.control_store.bootstrap_owner("owner", "Initial Owner", "a-long-test-password")

    async def login_and_me(client: httpx.AsyncClient):
        login = await client.post(
            "/auth/login",
            json={"login": "owner", "password": "a-long-test-password"},
            headers={"Origin": "http://test"},
        )
        me = await client.get("/auth/me")
        return login, me

    login, me = run(app, login_and_me)

    assert login.status_code == 200
    assert "HttpOnly" in login.headers["set-cookie"]
    assert "SameSite=lax" in login.headers["set-cookie"]
    assert login.json()["user"] == {
        "id": login.json()["user"]["id"],
        "login": "owner",
        "display_name": "Initial Owner",
    }
    assert me.status_code == 200
    assert me.json() == login.json()["user"]


def test_logout_revokes_session_immediately(tmp_path: Path):
    app = create_app(
        ServiceSettings(
            service="control-plane",
            database_path=tmp_path / "db.sqlite3",
            state_directory=tmp_path / "state",
        ),
        session_settings=ControlPlaneSessionSettings(
            cookie_secure=False, allowed_origin="http://test"
        ),
    )
    app.state.control_store.bootstrap_owner("owner", "Initial Owner", "a-long-test-password")

    async def request():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            login = await client.post(
                "/auth/login",
                json={"login": "owner", "password": "a-long-test-password"},
                headers={"Origin": "http://test"},
            )
            logout = await client.post(
                "/auth/logout",
                headers={"Origin": "http://test", "X-CSRF-Token": login.json()["csrf_token"]},
            )
            me = await client.get("/auth/me")
            return logout, me

    logout, me = asyncio.run(request())
    assert logout.status_code == 204
    assert me.status_code == 401


def test_session_recovers_csrf_and_rejects_missing_origin_or_csrf(tmp_path: Path):
    app = create_app(
        ServiceSettings(
            service="control-plane",
            database_path=tmp_path / "db.sqlite3",
            state_directory=tmp_path / "state",
        ),
        session_settings=ControlPlaneSessionSettings(
            cookie_secure=False, allowed_origin="http://test"
        ),
    )
    app.state.control_store.bootstrap_owner("owner", "Initial Owner", "a-long-test-password")

    async def request():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            login = await client.post(
                "/auth/login",
                json={"login": "owner", "password": "a-long-test-password"},
                headers={"Origin": "http://test"},
            )
            restored = await client.get("/auth/session")
            no_origin = await client.post(
                "/auth/logout", headers={"X-CSRF-Token": login.json()["csrf_token"]}
            )
            no_csrf = await client.post("/auth/logout", headers={"Origin": "http://test"})
            return login, restored, no_origin, no_csrf

    login, restored, no_origin, no_csrf = asyncio.run(request())
    assert (
        restored.status_code == 200 and restored.json()["csrf_token"] == login.json()["csrf_token"]
    )
    assert no_origin.status_code == no_csrf.status_code == 403


def test_password_change_revokes_old_sessions_and_expiry_is_enforced(organization):
    settings, store, _, _, _, _ = organization
    app = create_app(
        settings, ControlPlaneSessionSettings(cookie_secure=False, allowed_origin="http://test")
    )

    async def request():
        transport = httpx.ASGITransport(app=app)
        async with (
            httpx.AsyncClient(
                transport=transport, base_url="http://test", headers={"Origin": "http://test"}
            ) as first,
            httpx.AsyncClient(
                transport=transport, base_url="http://test", headers={"Origin": "http://test"}
            ) as second,
        ):
            for client in (first, second):
                login = await client.post(
                    "/auth/login",
                    json={"login": "owner", "password": "correct horse battery staple"},
                )
                client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
            old_cookie = dict(first.cookies)
            assert (
                await first.post(
                    "/auth/password",
                    json={
                        "current_password": "wrong password",
                        "new_password": "a new owner password",
                    },
                )
            ).status_code == 401
            assert (
                await first.post(
                    "/auth/password",
                    json={
                        "current_password": "correct horse battery staple",
                        "new_password": "short",
                    },
                )
            ).status_code == 422
            changed = await first.post(
                "/auth/password",
                json={
                    "current_password": "correct horse battery staple",
                    "new_password": "a new owner password",
                },
            )
            assert changed.status_code == 200
            assert (await second.get("/auth/session")).status_code == 401
            assert (await first.get("/auth/session")).status_code == 200
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test", cookies=old_cookie
            ) as old:
                assert (await old.get("/auth/me")).status_code == 401
            with store.connect() as connection:
                connection.execute("UPDATE sessions SET expires_at = 0")
            assert (await first.get("/auth/session")).status_code == 401
            assert (
                await first.post(
                    "/auth/login",
                    json={"login": "owner", "password": "correct horse battery staple"},
                )
            ).status_code == 401
            assert (
                await first.post(
                    "/auth/login", json={"login": "owner", "password": "a new owner password"}
                )
            ).status_code == 200

    asyncio.run(request())


def test_failed_login_is_throttled_without_more_password_work(organization, monkeypatch):
    from fesnyng_backend import control_store

    _, store, _, _, _, _ = organization
    for _ in range(control_store.LOGIN_MAX_FAILURES):
        assert store.login("owner", "wrong password", 3600) is None

    def unexpected_hash(*args):
        raise AssertionError("Blocked login must not perform expensive hashing")

    monkeypatch.setattr(control_store, "verify_password", unexpected_hash)
    assert store.login("owner", "correct horse battery staple", 3600) is None
