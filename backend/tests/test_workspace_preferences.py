import asyncio

import httpx

from fesnyng_backend.control_plane import create_app
from fesnyng_backend.settings import ControlPlaneSessionSettings

ORIGIN = "https://workspace.example"


def test_workspace_preferences_are_personal_durable_and_member_scoped(organization):
    settings, control, owner, org, _, _ = organization
    member = control.add_member(
        org.id,
        "member",
        "Member",
        "correct horse battery staple",
        "member",
        actor_id=owner.id,
    )
    other_org = control.create_organization(owner.id, "Other organization")
    app = create_app(settings, ControlPlaneSessionSettings(allowed_origin=ORIGIN))

    async def login(client: httpx.AsyncClient, name: str) -> None:
        response = await client.post(
            "/auth/login", json={"login": name, "password": "correct horse battery staple"}
        )
        assert response.status_code == 200
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]

    async def exercise() -> None:
        path = f"/organizations/{org.id}/workspace-preferences"
        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
            ) as owner_device,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
            ) as owner_second_device,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
            ) as member_device,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
            ) as anonymous,
        ):
            assert (await anonymous.get(path)).status_code == 401
            await login(owner_device, "owner")
            await login(owner_second_device, "owner")
            await login(member_device, "member")
            assert (await owner_device.get(path)).json() == {"thread_list_page_size": 6}
            assert (await owner_device.put(path, json={"thread_list_page_size": 12})).json() == {
                "thread_list_page_size": 12
            }
            assert (await owner_second_device.get(path)).json() == {"thread_list_page_size": 12}
            assert (await member_device.get(path)).json() == {"thread_list_page_size": 6}
            assert (await member_device.put(path, json={"thread_list_page_size": 24})).json() == {
                "thread_list_page_size": 24
            }
            assert (await owner_device.get(path)).json() == {"thread_list_page_size": 12}
            assert (
                await owner_device.put(
                    path,
                    headers={"X-CSRF-Token": "missing"},
                    json={"thread_list_page_size": 8},
                )
            ).status_code == 403
            for size in (0, 101):
                assert (
                    await owner_device.put(path, json={"thread_list_page_size": size})
                ).status_code == 422
            assert (await owner_device.get(path)).json() == {"thread_list_page_size": 12}
            assert (
                await member_device.get(f"/organizations/{other_org.id}/workspace-preferences")
            ).status_code == 404

    asyncio.run(exercise())
    control.remove_member(org.id, member.user_id, actor_id=owner.id)
    assert control.thread_list_page_size(org.id, member.user_id) == 6
    assert control.thread_list_page_size(org.id, owner.id) == 12
