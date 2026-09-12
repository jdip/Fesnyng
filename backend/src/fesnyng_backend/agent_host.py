"""Autonomous agent-host application entrypoint."""

from fastapi import FastAPI

from fesnyng_backend.application import create_service_app
from fesnyng_backend.settings import ServiceSettings, settings_from_environment


def create_app(settings: ServiceSettings | None = None) -> FastAPI:
    """Create the host API with state independent from the control plane."""

    resolved = settings or settings_from_environment("agent-host")
    if resolved.service != "agent-host":
        raise ValueError("Agent-host factory requires agent-host settings.")
    return create_service_app(resolved)
