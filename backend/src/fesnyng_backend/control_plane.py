"""Control-plane application entrypoint."""

from fastapi import FastAPI

from fesnyng_backend.agents import router as agents_router
from fesnyng_backend.application import create_service_app
from fesnyng_backend.auth import router as auth_router
from fesnyng_backend.control_codex_routes import router as codex_workspace_router
from fesnyng_backend.control_dispatch_routes import router as dispatch_router
from fesnyng_backend.control_docker_routes import router as docker_router
from fesnyng_backend.control_host_routes import router as host_router
from fesnyng_backend.control_interaction_routes import router as interaction_router
from fesnyng_backend.control_memory_routes import router as memory_router
from fesnyng_backend.control_peer_routes import router as peer_router
from fesnyng_backend.control_store import ControlPlaneStore
from fesnyng_backend.control_thread_acknowledgement_routes import (
    router as thread_acknowledgement_router,
)
from fesnyng_backend.control_thread_pin_routes import router as thread_pin_router
from fesnyng_backend.control_workspace_lifecycle_routes import router as workspace_lifecycle_router
from fesnyng_backend.control_workspace_preference_routes import (
    router as workspace_preference_router,
)
from fesnyng_backend.control_workspace_routes import router as workspace_router
from fesnyng_backend.departments import router as departments_router
from fesnyng_backend.organizations import router as organizations_router
from fesnyng_backend.projects import router as projects_router
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
    app.include_router(projects_router)
    app.include_router(agents_router)
    app.include_router(departments_router)
    app.include_router(host_router)
    app.include_router(docker_router)
    app.include_router(memory_router)
    app.include_router(dispatch_router)
    app.include_router(interaction_router)
    app.include_router(peer_router)
    app.include_router(thread_pin_router)
    app.include_router(thread_acknowledgement_router)
    app.include_router(workspace_preference_router)
    app.include_router(workspace_lifecycle_router)
    app.include_router(workspace_router)
    app.include_router(codex_workspace_router)
    return app
