import asyncio
from uuid import uuid4

import httpx

from fesnyng_backend.control_plane import create_app
from fesnyng_backend.settings import ControlPlaneSessionSettings, ServiceSettings


def test_native_proxy_schema_has_unique_stable_operations_and_preserves_auth(tmp_path):
    app = create_app(
        ServiceSettings(
            service="control-plane",
            database_path=tmp_path / "control.sqlite3",
            state_directory=tmp_path / "state",
        ),
        ControlPlaneSessionSettings(),
    )
    schema = app.openapi()
    operations = [
        operation["operationId"]
        for path in schema["paths"].values()
        for method, operation in path.items()
        if method in {"get", "post", "patch", "delete", "put", "head", "options", "trace"}
    ]
    assert len(operations) == len(set(operations))
    for harness in ("opencode", "codex"):
        path = f"/organizations/{{organization_id}}/agents/{{agent_id}}/{harness}/{{resource_path}}"
        assert {
            method: operation["operationId"] for method, operation in schema["paths"][path].items()
        } == {
            method: f"{harness}_workspace_{method}" for method in ("get", "post", "patch", "delete")
        }

    async def check_auth():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            prefix = f"/organizations/{uuid4()}/agents/{uuid4()}"
            for harness in ("opencode", "codex"):
                for method in ("GET", "POST", "PATCH", "DELETE"):
                    resource = "session" if method in {"GET", "POST"} else "session/ses_test"
                    response = await client.request(method, f"{prefix}/{harness}/{resource}")
                    assert response.status_code == 401

    asyncio.run(check_auth())
