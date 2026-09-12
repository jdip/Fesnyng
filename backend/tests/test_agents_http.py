import asyncio

from httpx import ASGITransport, AsyncClient

from fesnyng_backend.control_plane import create_app
from fesnyng_backend.settings import ControlPlaneSessionSettings

ORIGIN = "https://control.example"


async def sign_in(client: AsyncClient, login: str, password: str):
    response = await client.post("/auth/login", json={"login": login, "password": password})
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]


async def exercise_agent_roles(organization):
    settings, _, _, org, _, host_id = organization
    app = create_app(settings, ControlPlaneSessionSettings(allowed_origin=ORIGIN))
    async with AsyncClient(
        transport=ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
    ) as owner:
        await sign_in(owner, "owner", "correct horse battery staple")
        added = await owner.put(
            f"/organizations/{org.id}/members",
            json={
                "login": "member",
                "display_name": "Member",
                "password": "a separate member password",
                "role": "member",
            },
        )
        assert added.status_code == 200
        response = await owner.post(
            f"/organizations/{org.id}/agents",
            json={
                "name": "Engineer",
                "host_id": host_id,
            },
        )
        assert response.status_code == 201
        agent_id = response.json()["id"]
        assert response.json()["configuration_status"] == "pending"
        async with AsyncClient(
            transport=ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
        ) as member:
            await sign_in(member, "member", "a separate member password")
            assert (
                await member.get(f"/organizations/{org.id}/agents/{agent_id}")
            ).status_code == 200
            assert len((await member.get(f"/organizations/{org.id}/agents")).json()) == 1
            assert (
                await member.patch(
                    f"/organizations/{org.id}/agents/{agent_id}",
                    json={
                        "expected_version": 1,
                        "name": "Unpermitted",
                    },
                )
            ).status_code == 403
            assert (
                await member.put(
                    f"/organizations/{org.id}/policy",
                    json={
                        "expected_version": 1,
                        "configuration": {},
                    },
                )
            ).status_code == 403
            assert (
                await owner.delete(f"/organizations/{org.id}/members/{added.json()['user_id']}")
            ).status_code == 204
            assert (
                await member.get(f"/organizations/{org.id}/agents/{agent_id}")
            ).status_code == 404
        assert (
            await owner.patch(
                f"/organizations/{org.id}/agents/{agent_id}",
                json={
                    "expected_version": 1,
                    "name": "Senior engineer",
                },
            )
        ).status_code == 200
        assert (
            await owner.patch(
                f"/organizations/{org.id}/agents/{agent_id}",
                json={
                    "expected_version": 1,
                    "name": "Stale edit",
                },
            )
        ).status_code == 409


def test_agent_management_requires_current_organization_role(organization):
    asyncio.run(exercise_agent_roles(organization))


async def exercise_tenant_routes(organization):
    settings, control, owner_user, org, _, _ = organization
    other = control.create_organization(owner_user.id, "Separate organization")
    app = create_app(settings, ControlPlaneSessionSettings(allowed_origin=ORIGIN))
    paths = ["agents", "hosts", "profiles", "policy"]
    async with AsyncClient(
        transport=ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
    ) as owner:
        await sign_in(owner, "owner", "correct horse battery staple")
        assert (
            await owner.put(
                f"/organizations/{org.id}/members",
                json={
                    "login": "member",
                    "display_name": "Member",
                    "password": "a separate member password",
                    "role": "member",
                },
            )
        ).status_code == 200
        profile = await owner.post(
            f"/organizations/{org.id}/profiles", json={"name": "Primary profile"}
        )
        assert profile.status_code == 201
        assert len((await owner.get(f"/organizations/{org.id}/profiles")).json()) == 1
        assert len((await owner.get(f"/organizations/{org.id}/hosts")).json()) == 1
        assert (
            await owner.put(
                f"/organizations/{org.id}/policy",
                json={
                    "expected_version": 1,
                    "configuration": {"default_permission": "allow"},
                },
            )
        ).status_code == 200
        async with AsyncClient(
            transport=ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
        ) as member:
            for path in paths:
                assert (await member.get(f"/organizations/{other.id}/{path}")).status_code == 401
            await sign_in(member, "member", "a separate member password")
            for path in paths:
                assert (await member.get(f"/organizations/{other.id}/{path}")).status_code == 404
            assert (
                await member.post(f"/organizations/{org.id}/profiles", json={"name": "Unpermitted"})
            ).status_code == 403
        owner.headers.pop("X-CSRF-Token")
        assert (
            await owner.post(f"/organizations/{org.id}/profiles", json={"name": "Forged"})
        ).status_code == 403


def test_every_agent_resource_route_enforces_tenancy_and_write_authority(organization):
    asyncio.run(exercise_tenant_routes(organization))
