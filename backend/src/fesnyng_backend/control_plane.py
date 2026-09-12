"""Control-plane application entrypoint."""

from fastapi import FastAPI

from fesnyng_backend.application import create_service_app
from fesnyng_backend.settings import ServiceSettings, settings_from_environment


def create_app(settings: ServiceSettings | None = None) -> FastAPI:
    """Create the control plane without coupling it to an agent host."""

    resolved = settings or settings_from_environment("control-plane")
    if resolved.service != "control-plane":
        raise ValueError("Control-plane factory requires control-plane settings.")
    return create_service_app(resolved)
