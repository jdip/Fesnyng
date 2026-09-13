"""Control-plane application entrypoint."""

from fastapi import FastAPI

from fesnyng_backend.agents import router as agents_router
from fesnyng_backend.application import create_service_app
from fesnyng_backend.auth import router as auth_router
from fesnyng_backend.control_dispatch_routes import router as dispatch_router
from fesnyng_backend.control_host_routes import router as host_router
from fesnyng_backend.control_interaction_routes import router as interaction_router
from fesnyng_backend.control_memory_routes import router as memory_router
from fesnyng_backend.control_peer_routes import router as peer_router
from fesnyng_backend.control_store import ControlPlaneStore
from fesnyng_backend.organizations import router as organizations_router
from fesnyng_backend.settings import (
    ControlPlaneSessionSettings,
    ServiceSettings,
    control_plane_session_settings_from_environment,
    settings_from_environment,
)


def create_app(
    settings: ServiceSettings | None = None,
    session_settings: ControlPlaneSessionSettings | None = None,
) -> FastAPI:
    """Create the control plane without coupling it to an agent host."""

    resolved = settings or settings_from_environment("control-plane")
    if resolved.service != "control-plane":
        raise ValueError("Control-plane factory requires control-plane settings.")
    app = create_service_app(resolved)
    store = ControlPlaneStore(resolved.database_path)
    store.initialize()
    app.state.control_store = store
    app.state.session_settings = (
        session_settings or control_plane_session_settings_from_environment()
    )
    app.include_router(auth_router)
    app.include_router(organizations_router)
    app.include_router(agents_router)
    app.include_router(host_router)
    app.include_router(memory_router)
    app.include_router(dispatch_router)
    app.include_router(interaction_router)
    app.include_router(peer_router)
    return app
