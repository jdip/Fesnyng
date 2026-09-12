"""Host-owned device authorization and agent-only access credential endpoint."""

import asyncio
import time
from collections.abc import Mapping
from uuid import UUID

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from fesnyng_backend.agent_models import Contract, Name
from fesnyng_backend.host_credentials import OPENAI_AUTH_ISSUER, OPENAI_CODEX_CLIENT_ID
from fesnyng_backend.host_routes import host_errors, require_binding

router = APIRouter(tags=["host credentials"])


class ProfileRegistration(Contract):
    name: Name


@router.put("/organizations/{organization_id}/profiles/{profile_id}")
def register_profile(
    request: Request, organization_id: UUID, profile_id: UUID, body: ProfileRegistration
):
    org, profile = str(organization_id), str(profile_id)
    require_binding(request, org)
    with host_errors():
        request.app.state.credential_store.ensure_profile(org, profile, body.name)
        return request.app.state.credential_store.profile_status(org, profile)


@router.get("/organizations/{organization_id}/profiles/{profile_id}")
def profile_status(request: Request, organization_id: UUID, profile_id: UUID):
    org, profile = str(organization_id), str(profile_id)
    require_binding(request, org)
    status = request.app.state.credential_store.profile_status(org, profile)
    if status is None:
        raise HTTPException(404, "Host profile not found")
    return status


@router.get("/credential")
async def credential(request: Request):
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "Agent authentication required")
    try:
        value = await request.app.state.credential_service.access_for_agent(token)
    except PermissionError:
        raise HTTPException(403, "Host profile unavailable or agent unassigned") from None
    return JSONResponse(value, headers={"Cache-Control": "no-store"})


@router.post("/organizations/{organization_id}/profiles/{profile_id}/login")
async def begin_login(request: Request, organization_id: UUID, profile_id: UUID):
    org, profile = str(organization_id), str(profile_id)
    require_binding(request, org)
    store = request.app.state.credential_store
    operation = store.acquire_operation(
        org,
        profile,
        ["login_required", "ready", "refresh_uncertain", "account_mismatch"],
        "login_pending",
    )
    if operation is None:
        raise HTTPException(409, "Unknown profile or credential operation already in progress")
    client = request.app.state.provider_client
    try:
        response = await client.post(
            f"{OPENAI_AUTH_ISSUER}/api/accounts/deviceauth/usercode",
            json={"client_id": OPENAI_CODEX_CLIENT_ID},
        )
        response.raise_for_status()
        device_response = response.json()
        if not isinstance(device_response, Mapping):
            raise TypeError("Invalid device authorization response")
        user_code = device_response.get("user_code")
        device_auth_id = device_response.get("device_auth_id")
        if not isinstance(user_code, str) or not isinstance(device_auth_id, str):
            raise TypeError("Invalid device authorization response")
        device = {"user_code": user_code, "device_auth_id": device_auth_id}
        interval = max(int(device_response.get("interval", 5)), 1) + 3
    except asyncio.CancelledError:
        _cancel_login(store, org, profile, operation)
        raise
    except (httpx.HTTPError, ValueError, TypeError):
        _cancel_login(store, org, profile, operation)
        raise HTTPException(503, "Provider device authorization unavailable") from None
    task = asyncio.create_task(
        _complete_login(request.app.state, org, profile, operation, device, interval)
    )
    request.app.state.login_tasks.add(task)
    task.add_done_callback(request.app.state.login_tasks.discard)
    return {
        "verification_uri": f"{OPENAI_AUTH_ISSUER}/codex/device",
        "user_code": device["user_code"],
        "expires_at": int(time.time()) + 900,
    }


def _cancel_login(store, organization_id: str, profile_id: str, operation: str) -> None:
    with store.connect() as connection:
        connection.execute(
            "UPDATE credential_profiles SET state='login_required',operation='' WHERE organization_id=? AND profile_id=? AND operation=? AND state='login_pending'",
            (organization_id, profile_id, operation),
        )


async def _complete_login(
    state, org: str, profile: str, operation: str, device: dict, interval: int
) -> None:
    exchanging = False
    try:
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            response = await state.provider_client.post(
                f"{OPENAI_AUTH_ISSUER}/api/accounts/deviceauth/token",
                json={"device_auth_id": device["device_auth_id"], "user_code": device["user_code"]},
            )
            if response.is_success:
                grant = response.json()
                exchanging = True
                result = await state.provider_client.post(
                    f"{OPENAI_AUTH_ISSUER}/oauth/token",
                    data={
                        "grant_type": "authorization_code",
                        "code": grant["authorization_code"],
                        "redirect_uri": f"{OPENAI_AUTH_ISSUER}/deviceauth/callback",
                        "client_id": OPENAI_CODEX_CLIENT_ID,
                        "code_verifier": grant["code_verifier"],
                    },
                )
                result.raise_for_status()
                state.credential_store.save_tokens(org, profile, result.json(), operation)
                return
            if response.status_code not in (403, 404):
                break
            await asyncio.sleep(interval)
    except (
        httpx.HTTPError,
        ValueError,
        TypeError,
        KeyError,
        PermissionError,
        RuntimeError,
        asyncio.CancelledError,
    ):
        pass
    finally:
        if exchanging:
            state.credential_store.mark_uncertain(org, profile, operation)
        else:
            _cancel_login(state.credential_store, org, profile, operation)
