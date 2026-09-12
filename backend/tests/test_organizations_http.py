import asyncio
from pathlib import Path

import httpx

from fesnyng_backend.control_plane import create_app
from fesnyng_backend.settings import ControlPlaneSessionSettings, ServiceSettings


def test_owner_can_create_and_list_an_organization(tmp_path: Path):
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
            csrf = login.json()["csrf_token"]
            created = await client.post(
                "/organizations",
                json={"name": "Example"},
                headers={"Origin": "http://test", "X-CSRF-Token": csrf},
            )
            listed = await client.get("/organizations")
            return created, listed

    created, listed = asyncio.run(request())

    assert created.status_code == 200
    assert listed.status_code == 200
    assert listed.json() == [created.json()]


def test_member_cannot_manage_or_read_another_organization(tmp_path: Path):
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
    owner = app.state.control_store.bootstrap_owner(
        "owner", "Initial Owner", "a-long-test-password"
    )
    first = app.state.control_store.create_organization(owner.id, "First")
    second = app.state.control_store.create_organization(owner.id, "Second")
    app.state.control_store.add_member(
        first.id, "member", "Member", "a-long-test-password", "member", actor_id=owner.id
    )

    async def request():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            login = await client.post(
                "/auth/login",
                json={"login": "member", "password": "a-long-test-password"},
                headers={"Origin": "http://test"},
            )
            csrf = login.json()["csrf_token"]
            manage = await client.put(
                f"/organizations/{first.id}/members",
                json={
                    "login": "other",
                    "display_name": "Other",
                    "password": "a-long-test-password",
                    "role": "member",
                },
                headers={"Origin": "http://test", "X-CSRF-Token": csrf},
            )
            hidden = await client.get(f"/organizations/{second.id}")
            return manage, hidden

    manage, hidden = asyncio.run(request())
    assert manage.status_code == 403
    assert hidden.status_code == 404


def test_manager_receives_422_for_an_invalid_member_login(tmp_path: Path):
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
    owner = app.state.control_store.bootstrap_owner(
        "owner", "Initial Owner", "a-long-test-password"
    )
    organization = app.state.control_store.create_organization(owner.id, "Example")

    async def request():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            login = await client.post(
                "/auth/login",
                json={"login": "owner", "password": "a-long-test-password"},
                headers={"Origin": "http://test"},
            )
            return await client.put(
                f"/organizations/{organization.id}/members",
                json={
                    "login": "!",
                    "display_name": "Invalid",
                    "password": "a-long-test-password",
                    "role": "member",
                },
                headers={"Origin": "http://test", "X-CSRF-Token": login.json()["csrf_token"]},
            )

    assert asyncio.run(request()).status_code == 422


def test_admin_cannot_change_or_remove_owner_and_existing_password_is_not_overwritten(
    tmp_path: Path,
):
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
    owner = app.state.control_store.bootstrap_owner("owner", "Owner", "a-long-test-password")
    org = app.state.control_store.create_organization(owner.id, "Example")
    app.state.control_store.add_member(
        org.id, "admin", "Admin", "a-long-test-password", "admin", actor_id=owner.id
    )

    async def request():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            login = await client.post(
                "/auth/login",
                json={"login": "admin", "password": "a-long-test-password"},
                headers={"Origin": "http://test"},
            )
            headers = {"Origin": "http://test", "X-CSRF-Token": login.json()["csrf_token"]}
            grant = await client.put(
                f"/organizations/{org.id}/members",
                json={"login": "admin", "display_name": "Admin", "role": "owner"},
                headers=headers,
            )
            demote = await client.put(
                f"/organizations/{org.id}/members",
                json={"login": "owner", "display_name": "Owner", "role": "member"},
                headers=headers,
            )
            remove = await client.delete(
                f"/organizations/{org.id}/members/{owner.id}", headers=headers
            )
            overwrite = await client.put(
                f"/organizations/{org.id}/members",
                json={
                    "login": "owner",
                    "display_name": "Owner",
                    "password": "different-long-password",
                    "role": "owner",
                },
                headers=headers,
            )
            return grant, demote, remove, overwrite

    assert [x.status_code for x in asyncio.run(request())] == [403, 403, 403, 403]


def test_owner_can_add_existing_users_without_replacing_their_password(organization):
    settings, store, owner, first, _, _ = organization
    second = store.create_organization(owner.id, "Second")
    app = create_app(
        settings, ControlPlaneSessionSettings(cookie_secure=False, allowed_origin="http://test")
    )

    async def request():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Origin": "http://test"},
        ) as client:
            login = await client.post(
                "/auth/login", json={"login": "owner", "password": "correct horse battery staple"}
            )
            client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
            created = await client.put(
                f"/organizations/{first.id}/members",
                json={
                    "login": "member",
                    "display_name": "Member",
                    "password": "original member password",
                    "role": "member",
                },
            )
            assert created.status_code == 200
            replacement = await client.put(
                f"/organizations/{second.id}/members",
                json={
                    "login": "member",
                    "display_name": "Member",
                    "password": "replacement member password",
                    "role": "member",
                },
            )
            assert replacement.status_code == 422
            shared = await client.put(
                f"/organizations/{second.id}/members",
                json={"login": "member", "display_name": "Member", "role": "member"},
            )
            assert shared.status_code == 200
            assert shared.json()["user_id"] == created.json()["user_id"]
            assert (
                await client.delete(f"/organizations/{first.id}/members/{owner.id}")
            ).status_code == 409
            assert (
                await client.post(
                    "/auth/login", json={"login": "member", "password": "original member password"}
                )
            ).status_code == 200

    asyncio.run(request())
