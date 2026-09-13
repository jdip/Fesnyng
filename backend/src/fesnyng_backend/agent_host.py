"""Autonomous agent-host application entrypoint."""

import asyncio
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from fesnyng_backend.agent_lifecycle import AgentLifecycle
from fesnyng_backend.application import create_service_app
from fesnyng_backend.host_auth_routes import router as credential_router
from fesnyng_backend.host_configuration import HostConfiguration
from fesnyng_backend.host_credentials import CredentialService, CredentialStore
from fesnyng_backend.host_dispatch import Dispatcher, DispatchStore
from fesnyng_backend.host_dispatch_resolution import DispatchResolutionService
from fesnyng_backend.host_dispatch_routes import router as dispatch_router
from fesnyng_backend.host_interaction_routes import router as interaction_router
from fesnyng_backend.host_interactions import Interactions
from fesnyng_backend.host_lifecycle import exclusive_host
from fesnyng_backend.host_mcp import create_memory_mcp, register_collaboration_tools
from fesnyng_backend.host_memory import MemoryStore
from fesnyng_backend.host_memory_routes import router as memory_router
from fesnyng_backend.host_routes import router
from fesnyng_backend.host_runtime import DockerRuntime
from fesnyng_backend.host_store import HostStore
from fesnyng_backend.host_workspace_routes import router as workspace_router
from fesnyng_backend.peer_configuration import PeerConfigurationStore
from fesnyng_backend.peer_configuration_routes import router as peer_configuration_router
from fesnyng_backend.peer_delivery import PeerDeliveryService
from fesnyng_backend.peer_delivery_routes import router as peer_delivery_router
from fesnyng_backend.peer_discovery import PeerDiscovery
from fesnyng_backend.peer_discovery_routes import router as peer_discovery_router
from fesnyng_backend.settings import ServiceSettings, settings_from_environment


def create_app(settings: ServiceSettings | None = None) -> FastAPI:
    """Create the host API with state independent from the control plane."""

    resolved = settings or settings_from_environment("agent-host")
    if resolved.service != "agent-host":
        raise ValueError("Agent-host factory requires agent-host settings.")
    app = create_service_app(resolved)
    store = HostStore(resolved)
    store.initialize()
    app.state.host_store = store
    app.state.host_memory = MemoryStore(store)
    app.state.host_memory.initialize()
    app.state.host_runtime = DockerRuntime(
        store,
        os.environ.get("FESNYNG_AGENT_HOST_CREDENTIAL_URL", "http://host.lima.internal:8001"),
        os.environ.get("FESNYNG_AGENT_HOST_IMAGE", "fesnyng-agent:local"),
    )
    app.state.dispatch_store = DispatchStore(store)
    app.state.dispatch_store.initialize()
    app.state.interactions = Interactions(store, app.state.host_runtime)
    app.state.interactions.initialize()
    app.state.dispatcher = Dispatcher(
        app.state.dispatch_store, app.state.host_runtime, app.state.interactions
    )
    app.state.interactions.native_admissions_settled = (
        app.state.dispatcher.policy_admissions_settled
    )
    app.state.dispatch_resolution = DispatchResolutionService(
        store, app.state.dispatch_store, app.state.dispatcher, app.state.host_runtime
    )
    app.state.peer_configuration = PeerConfigurationStore(store)
    app.state.peer_configuration.initialize()
    app.state.peer_discovery = PeerDiscovery(
        store, app.state.peer_configuration, app.state.host_runtime
    )
    app.state.peer_delivery = PeerDeliveryService(
        store,
        app.state.peer_configuration,
        app.state.host_runtime,
        app.state.dispatch_store,
        app.state.dispatcher,
    )
    app.state.peer_delivery.initialize()
    mcp_server, mcp_app = create_memory_mcp(
        store, app.state.host_memory, app.state.host_runtime.credential_url
    )
    register_collaboration_tools(
        mcp_server, store, app.state.peer_discovery, app.state.peer_delivery
    )
    app.state.mcp_server = mcp_server
    app.mount("/mcp", mcp_app)
    credentials = CredentialStore(resolved.database_path)
    credentials.initialize()
    app.state.credential_store = credentials
    app.state.login_tasks = set()
    app.state.host_configuration = HostConfiguration(
        store, app.state.host_runtime, credentials, app.state.interactions, app.state.dispatch_store
    )
    app.state.agent_lifecycle = AgentLifecycle(
        store,
        app.state.host_runtime,
        app.state.dispatcher,
        app.state.host_configuration,
    )

    async def reconcile_configuration():
        while True:
            await app.state.host_configuration.reconcile_once()
            await app.state.interactions.reconcile_once()
            await asyncio.sleep(1)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        with exclusive_host(resolved.database_path):
            credentials.recover_interrupted()
            application.state.interactions.recover_interrupted()
            application.state.agent_lifecycle.recover_interrupted()
            async with (
                httpx.AsyncClient(
                    timeout=30, follow_redirects=False, headers={"User-Agent": "opencode/1.18.30"}
                ) as client,
                mcp_server.session_manager.run(),
                application.state.dispatcher.run(),
                application.state.peer_delivery.run(),
                asyncio.TaskGroup() as background,
            ):
                application.state.provider_client = client
                application.state.credential_service = CredentialService(credentials, client)
                reconciliation = background.create_task(reconcile_configuration())
                try:
                    yield
                finally:
                    reconciliation.cancel()
                    tasks = list(application.state.login_tasks)
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)

    app.router.lifespan_context = lifespan
    app.include_router(router)
    app.include_router(credential_router)
    app.include_router(memory_router)
    app.include_router(dispatch_router)
    app.include_router(interaction_router)
    app.include_router(peer_configuration_router)
    app.include_router(peer_delivery_router)
    app.include_router(peer_discovery_router)
    app.include_router(workspace_router)
    return app
