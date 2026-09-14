import asyncio

import httpx

from fesnyng_backend import control_host_routes
from fesnyng_backend.control_plane import create_app
from fesnyng_backend.host_client import HostClient
from fesnyng_backend.settings import ControlPlaneSessionSettings
from test_control_hosts import ORIGIN, _configured_agent, _sign_in


def test_generic_history_url_preserves_uuid_shaped_native_ids(organization, monkeypatch):
    settings, _, _, org, _, _ = organization
    agents, _, agent, _ = _configured_agent(organization)
    thread_id = "12345678-1234-1234-1234-123456789abc"

    def respond(request):
        assert request.url.path.endswith(f"/sessions/{thread_id}/messages")
        return httpx.Response(
            200, json={"thread": {"id": thread_id}, "turns": [], "historyState": "complete"}
        )

    host = HostClient(agents, transport=httpx.MockTransport(respond))
    monkeypatch.setattr(control_host_routes, "host_client", lambda _: host)
    app = create_app(settings, ControlPlaneSessionSettings(allowed_origin=ORIGIN))

    async def read():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
        ) as client:
            await _sign_in(client, "owner", "correct horse battery staple")
            response = await client.get(
                f"/organizations/{org.id}/agents/{agent['id']}/sessions/{thread_id}/messages"
            )
            assert response.status_code == 200
            assert response.json()["thread"]["id"] == thread_id

    asyncio.run(read())
