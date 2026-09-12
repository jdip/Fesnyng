"""Autonomous agent-host application entrypoint."""

import asyncio
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from fesnyng_backend.application import create_service_app
from fesnyng_backend.host_auth_routes import router as credential_router
from fesnyng_backend.host_credentials import CredentialService, CredentialStore
from fesnyng_backend.host_routes import router
from fesnyng_backend.host_runtime import DockerRuntime
from fesnyng_backend.host_store import HostStore
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
    app.state.host_runtime = DockerRuntime(
        store,
        os.environ.get("FESNYNG_AGENT_HOST_CREDENTIAL_URL", "http://host.lima.internal:8001"),
        os.environ.get("FESNYNG_AGENT_HOST_IMAGE", "fesnyng-agent:local"),
    )
    credentials = CredentialStore(resolved.database_path)
    credentials.initialize()
    app.state.credential_store = credentials
    app.state.login_tasks = set()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        async with httpx.AsyncClient(
            timeout=30, follow_redirects=False, headers={"User-Agent": "opencode/1.18.30"}
        ) as client:
            application.state.provider_client = client
            application.state.credential_service = CredentialService(credentials, client)
            try:
                yield
            finally:
                tasks = list(application.state.login_tasks)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    app.router.lifespan_context = lifespan
    app.include_router(router)
    app.include_router(credential_router)
    return app
