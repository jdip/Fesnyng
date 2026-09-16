"""Private, loopback-only host maintenance controls."""

import ipaddress
import os
import secrets
import stat
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from fesnyng_backend.host_maintenance import MaintenanceAlreadyActive, MaintenanceBusy

router = APIRouter(tags=["host maintenance"])


def _private_token() -> str | None:
    path = os.environ.get("FESNYNG_MAINTENANCE_TOKEN_FILE")
    if path:
        try:
            token_file = Path(path)
            metadata = token_file.stat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_mode & 0o077
                or metadata.st_uid != os.geteuid()
            ):
                return None
            token = token_file.read_text(encoding="utf-8").strip()
        except OSError:
            return None
    else:
        token = os.environ.get("FESNYNG_MAINTENANCE_TOKEN", "")
    return token if len(token) >= 32 else None


def require_maintenance_access(request: Request) -> None:
    client = request.client
    try:
        local = client is not None and ipaddress.ip_address(client.host).is_loopback
    except ValueError:
        local = False
    if not local:
        raise HTTPException(403, "Host maintenance is available only from loopback")
    expected = _private_token()
    if expected is None:
        raise HTTPException(503, "Host maintenance token is unavailable")
    scheme, _, supplied = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(401, "Host maintenance authentication required")


@router.get("/maintenance")
def status(request: Request):
    require_maintenance_access(request)
    return request.app.state.maintenance_guard.store.maintenance_status()


@router.post("/maintenance/acquire")
async def acquire(request: Request):
    require_maintenance_access(request)
    try:
        return await request.app.state.maintenance_guard.acquire()
    except MaintenanceAlreadyActive:
        return JSONResponse(
            status_code=409, content={"state": "closed", "reason": "maintenance already active"}
        )
    except MaintenanceBusy as error:
        return JSONResponse(status_code=409, content={"state": "open", "reason": str(error)})


@router.post("/maintenance/release")
def release(request: Request):
    require_maintenance_access(request)
    return request.app.state.maintenance_guard.release()
