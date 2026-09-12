"""Browser authentication backed by opaque, revocable SQLite sessions."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from fesnyng_backend.control_store import ControlPlaneStore, Membership, User
from fesnyng_backend.settings import ControlPlaneSessionSettings

router = APIRouter(prefix="/auth", tags=["authentication"])


class LoginRequest(BaseModel):
    login: str = Field(max_length=64)
    password: str = Field(max_length=256)


class UserResponse(BaseModel):
    id: str
    login: str
    display_name: str


class LoginResponse(BaseModel):
    user: UserResponse
    csrf_token: str


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(max_length=256)


def get_store(request: Request) -> ControlPlaneStore:
    return request.app.state.control_store


def get_session_settings(request: Request) -> ControlPlaneSessionSettings:
    return request.app.state.session_settings


def current_user(request: Request) -> User:
    settings = get_session_settings(request)
    token = request.cookies.get(settings.cookie_name)
    user = get_store(request).user_for_session(token) if token else None
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return user


def require_member(request: Request, organization_id: str) -> Membership:
    user = current_user(request)
    try:
        return get_store(request).membership_for(user.id, organization_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Organization not found.") from None


def require_manager(request: Request, organization_id: str) -> Membership:
    membership = require_member(request, organization_id)
    if membership.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Organization management is not permitted.")
    return membership


def require_unsafe_request(request: Request) -> User:
    _require_allowed_origin(request)
    user = current_user(request)
    settings = get_session_settings(request)
    token = request.cookies.get(settings.cookie_name)
    csrf_token = request.headers.get("X-CSRF-Token")
    if (
        token is None
        or csrf_token is None
        or not get_store(request).session_matches_csrf(token, csrf_token)
    ):
        raise HTTPException(status_code=403, detail="CSRF validation failed.")
    return user


@router.post("/login", response_model=LoginResponse)
def login(request: Request, body: LoginRequest, response: Response) -> LoginResponse:
    _require_allowed_origin(request)
    store = get_store(request)
    settings = get_session_settings(request)
    authenticated = store.login(body.login, body.password, settings.session_lifetime_seconds)
    if authenticated is None:
        raise HTTPException(status_code=401, detail="Invalid login or password.")
    user, session = authenticated
    response.set_cookie(
        key=settings.cookie_name,
        value=session.token,
        max_age=settings.session_lifetime_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
    )
    return LoginResponse(user=UserResponse(**user.__dict__), csrf_token=session.csrf_token)


@router.get("/me", response_model=UserResponse)
def me(user: Annotated[User, Depends(current_user)]) -> UserResponse:
    return UserResponse(**user.__dict__)


@router.get("/session", response_model=LoginResponse)
def session(request: Request, user: Annotated[User, Depends(current_user)]) -> LoginResponse:
    token = request.cookies.get(get_session_settings(request).cookie_name)
    csrf_token = get_store(request).csrf_for_session(token) if token else None
    if csrf_token is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return LoginResponse(user=UserResponse(**user.__dict__), csrf_token=csrf_token)


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response) -> Response:
    user = require_unsafe_request(request)
    del user
    settings = get_session_settings(request)
    token = request.cookies.get(settings.cookie_name)
    if token is not None:
        get_store(request).revoke_session(token)
    response.delete_cookie(
        settings.cookie_name, secure=settings.cookie_secure, httponly=True, samesite="lax"
    )
    response.status_code = 204
    return response


@router.post("/password", response_model=LoginResponse)
def change_password(
    request: Request, body: PasswordChangeRequest, response: Response
) -> LoginResponse:
    user = require_unsafe_request(request)
    settings = get_session_settings(request)
    try:
        session = get_store(request).change_password(
            user.id, body.current_password, body.new_password, settings.session_lifetime_seconds
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    if session is None:
        raise HTTPException(status_code=401, detail="Invalid login or password.")
    response.set_cookie(
        key=settings.cookie_name,
        value=session.token,
        max_age=settings.session_lifetime_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
    )
    return LoginResponse(user=UserResponse(**user.__dict__), csrf_token=session.csrf_token)


def _require_allowed_origin(request: Request) -> None:
    settings = get_session_settings(request)
    origin = request.headers.get("Origin")
    if settings.allowed_origin is not None and origin != settings.allowed_origin:
        raise HTTPException(status_code=403, detail="Origin is not allowed.")
    if settings.allowed_origin is None and origin is not None:
        raise HTTPException(status_code=403, detail="Origin is not allowed.")
