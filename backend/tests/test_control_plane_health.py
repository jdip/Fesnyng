import asyncio
from pathlib import Path

import httpx

from fesnyng_backend.control_plane import create_app
from fesnyng_backend.settings import ServiceSettings


def get_health(app):
    async def request():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/health")

    return asyncio.run(request())


def test_control_plane_health_reports_durable_identity(tmp_path: Path):
    settings = ServiceSettings(
        service="control-plane",
        database_path=tmp_path / "control-plane.sqlite3",
        state_directory=tmp_path / "state",
    )

    first = get_health(create_app(settings))
    restarted = get_health(create_app(settings))

    assert first.status_code == 200
    assert first.json() == {
        "status": "ok",
        "service": "control-plane",
        "instance_id": first.json()["instance_id"],
        "schema_version": 1,
    }
    assert restarted.json()["instance_id"] == first.json()["instance_id"]
