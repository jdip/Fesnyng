import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from fastapi.responses import StreamingResponse

from fesnyng_backend.settings import ControlPlaneSessionSettings, ServiceSettings
from fesnyng_backend.web import create_app

ORIGIN = "https://fesnyng.example.test"


def web_app(tmp_path: Path, revision: str | None = None):
    frontend = tmp_path / "frontend"
    (frontend / "assets").mkdir(parents=True)
    (frontend / "index.html").write_text("<main>Fesnyng workspace</main>")
    (frontend / "assets" / "app.js").write_text("console.log('workspace')")
    settings = ServiceSettings(
        service="control-plane",
        database_path=tmp_path / "control.sqlite3",
        state_directory=tmp_path / "state",
    )
    app = create_app(
        settings,
        ControlPlaneSessionSettings(allowed_origin=ORIGIN),
        frontend,
        revision,
    )
    app.state.control_plane.state.control_store.bootstrap_owner(
        "owner", "Owner", "correct horse battery staple"
    )
    return app


def request(app, method: str, path: str, **kwargs):
    async def send():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url=ORIGIN, follow_redirects=False
        ) as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(send())


def test_web_app_serves_compiled_workspace_and_preserves_api_boundary(tmp_path: Path):
    app = web_app(tmp_path, "a" * 40)

    index = request(app, "GET", "/")
    workspace = request(app, "GET", "/organizations/example")
    asset = request(app, "GET", "/assets/app.js")
    asset_head = request(app, "HEAD", "/assets/app.js")
    missing_asset = request(app, "GET", "/assets/missing.js")
    api_root = request(app, "GET", "/api")
    missing_api = request(app, "GET", "/api/not-a-route")
    api_health = request(app, "GET", "/api/health")
    health = request(app, "GET", "/health")

    assert index.text == workspace.text == "<main>Fesnyng workspace</main>"
    assert asset.text == "console.log('workspace')"
    assert asset_head.status_code == 200
    assert missing_asset.status_code == api_root.status_code == missing_api.status_code == 404
    assert api_health.json()["service"] == "control-plane"
    assert api_health.json()["revision"] == health.json()["revision"] == "a" * 40


def test_web_app_keeps_session_cookie_at_the_browser_origin(tmp_path: Path):
    app = web_app(tmp_path)

    async def login_and_restore():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
            login = await client.post(
                "/api/auth/login",
                headers={"Origin": ORIGIN},
                json={"login": "owner", "password": "correct horse battery staple"},
            )
            session = await client.get("/api/auth/session")
            rejected = await client.post(
                "/api/auth/logout", headers={"Origin": "https://elsewhere.example.test"}
            )
            return login, session, rejected

    login, session, rejected = asyncio.run(login_and_restore())

    assert login.status_code == session.status_code == 200
    assert "HttpOnly" in login.headers["set-cookie"]
    assert "Path=/" in login.headers["set-cookie"]
    assert "Secure" in login.headers["set-cookie"]
    assert rejected.status_code == 403


def test_web_app_forwards_control_plane_event_streams(tmp_path: Path):
    app = web_app(tmp_path)

    async def events():
        yield b"event: workspace.updated\ndata: {}\n\n"

    app.state.control_plane.add_api_route(
        "/events", lambda: StreamingResponse(events(), media_type="text/event-stream")
    )

    response = request(app, "GET", "/api/events")

    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.content == b"event: workspace.updated\ndata: {}\n\n"


def test_web_app_rejects_static_path_escapes(tmp_path: Path):
    app = web_app(tmp_path)
    (tmp_path / "secret.txt").write_text("must not serve")

    escaped = request(app, "GET", "/assets/%2e%2e/secret.txt")

    assert escaped.status_code == 404


def test_web_app_reads_its_release_inputs_from_environment(tmp_path: Path, monkeypatch):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("<main>Released workspace</main>")
    revision = "b" * 40
    monkeypatch.setenv("FESNYNG_FRONTEND_DIST", str(frontend))
    monkeypatch.setenv("FESNYNG_DEPLOYED_REVISION", revision)
    settings = ServiceSettings(
        service="control-plane",
        database_path=tmp_path / "control.sqlite3",
        state_directory=tmp_path / "state",
    )

    app = create_app(settings, ControlPlaneSessionSettings(allowed_origin=ORIGIN))

    assert request(app, "GET", "/health").json()["revision"] == revision


def test_web_app_rejects_an_ambiguous_release_revision(tmp_path: Path):
    with pytest.raises(ValueError, match="40-character Git SHA"):
        web_app(tmp_path, "not-a-revision")


def test_web_app_forwards_the_control_plane_lifespan(tmp_path: Path):
    app = web_app(tmp_path)
    events = []

    @asynccontextmanager
    async def control_lifespan(control):
        assert control is app.state.control_plane
        events.append("started")
        yield
        events.append("stopped")

    app.state.control_plane.router.lifespan_context = control_lifespan

    async def run_lifespan():
        async with app.router.lifespan_context(app):
            assert events == ["started"]

    asyncio.run(run_lifespan())

    assert events == ["started", "stopped"]
