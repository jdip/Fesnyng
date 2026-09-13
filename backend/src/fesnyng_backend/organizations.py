"""Organization membership API with tenant-scoped authorization."""

from __future__ import annotations

import base64
import re
import unicodedata
import warnings
from io import BytesIO
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field

from fesnyng_backend import auth
from fesnyng_backend.agent_models import Contract
from fesnyng_backend.control_store import Membership, Organization

router = APIRouter(prefix="/organizations", tags=["organizations"])

_IMAGE_DATA_URL = re.compile(
    r"^data:(image/png|image/jpeg|image/webp);base64,([A-Za-z0-9+/]+={0,2})$"
)
_IMAGE_FORMATS = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}
_MAX_IMAGE_BYTES = 2 * 1024 * 1024
_MAX_IMAGE_PIXELS = 4_000_000
_MAX_ICON_EDGE = 128
_KEYCAP_EMOJI = re.compile(r"^[0-9#*]\ufe0f?\u20e3$")


class OrganizationIcon(BaseModel):
    kind: Literal["emoji", "image"]
    value: str


class OrganizationResponse(BaseModel):
    id: str
    name: str
    created_by_user_id: str
    icon: OrganizationIcon | None = None


class CreateOrganizationRequest(BaseModel):
    name: str = Field(max_length=120)


class OrganizationIconRequest(Contract):
    kind: Literal["emoji", "image"]
    value: str = Field(max_length=2_900_000)


class UpdateOrganizationIconRequest(Contract):
    icon: OrganizationIconRequest | None


class MemberRequest(BaseModel):
    login: str = Field(max_length=64)
    display_name: str = Field(max_length=100)
    password: str | None = Field(default=None, max_length=256)
    role: str


class MembershipResponse(BaseModel):
    user_id: str
    organization_id: str
    role: str
    login: str | None = None
    display_name: str | None = None


@router.post("", response_model=OrganizationResponse)
def create_organization(request: Request, body: CreateOrganizationRequest) -> OrganizationResponse:
    user = auth.require_unsafe_request(request)
    try:
        organization = auth.get_store(request).create_organization(user.id, body.name)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    return _organization_response(organization)


@router.get("", response_model=list[OrganizationResponse])
def list_organizations(request: Request) -> list[OrganizationResponse]:
    user = auth.current_user(request)
    return [
        _organization_response(item) for item in auth.get_store(request).list_organizations(user.id)
    ]


@router.get("/{organization_id}", response_model=OrganizationResponse)
def get_organization(request: Request, organization_id: str) -> OrganizationResponse:
    user = auth.current_user(request)
    try:
        organization = auth.get_store(request).organization_for_member(user.id, organization_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Organization not found.") from None
    return _organization_response(organization)


@router.put("/{organization_id}/icon", response_model=OrganizationResponse)
def update_organization_icon(
    request: Request, organization_id: str, body: UpdateOrganizationIconRequest
) -> OrganizationResponse:
    user = auth.require_unsafe_request(request)
    auth.require_manager(request, organization_id)
    try:
        icon_kind, icon_value = _validated_icon(body.icon)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    store = auth.get_store(request)
    store.set_organization_icon(organization_id, icon_kind, icon_value)
    return _organization_response(store.organization_for_member(user.id, organization_id))


@router.get("/{organization_id}/members", response_model=list[MembershipResponse])
def list_members(request: Request, organization_id: str) -> list[MembershipResponse]:
    auth.require_member(request, organization_id)
    return [
        MembershipResponse(**membership.__dict__, login=user.login, display_name=user.display_name)
        for membership, user in auth.get_store(request).list_memberships(organization_id)
    ]


@router.put("/{organization_id}/members", response_model=MembershipResponse)
def add_member(request: Request, organization_id: str, body: MemberRequest) -> MembershipResponse:
    actor = auth.require_unsafe_request(request)
    try:
        membership = auth.get_store(request).add_member(
            organization_id,
            body.login,
            body.display_name,
            body.password,
            body.role,
            actor_id=actor.id,
        )
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from None
    except LookupError:
        raise HTTPException(status_code=404, detail="Organization not found.") from None
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    return _membership_response(membership)


@router.delete("/{organization_id}/members/{user_id}", status_code=204)
def remove_member(request: Request, organization_id: str, user_id: str) -> None:
    actor = auth.require_unsafe_request(request)
    try:
        auth.get_store(request).remove_member(organization_id, user_id, actor_id=actor.id)
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from None
    except LookupError:
        raise HTTPException(status_code=404, detail="Organization not found.") from None
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None


def _validated_icon(icon: OrganizationIconRequest | None) -> tuple[str | None, str | None]:
    if icon is None:
        return None, None
    if icon.kind == "emoji":
        return icon.kind, _validated_emoji(icon.value)
    return icon.kind, _normalized_image(icon.value)


def _validated_emoji(value: str) -> str:
    if not value or value != value.strip() or len(value) > 16:
        raise ValueError("Organization icon emoji must be a short nonempty symbol")
    if _KEYCAP_EMOJI.fullmatch(value):
        return value
    categories = [unicodedata.category(character) for character in value]
    allowed = {"So", "Sk", "Mn", "Me"}
    allowed_format = {"\u200d", *map(chr, range(0xE0020, 0xE0080))}
    if (
        "So" not in categories
        or value[0] == "\u200d"
        or value[-1] == "\u200d"
        or any(
            category not in allowed and character not in allowed_format
            for character, category in zip(value, categories, strict=True)
        )
    ):
        raise ValueError("Organization icon emoji must be a short nonempty symbol")
    return value


def _normalized_image(value: str) -> str:
    match = _IMAGE_DATA_URL.fullmatch(value)
    if match is None:
        raise ValueError("Organization icon image must be a PNG, JPEG, or WebP data URL")
    media_type, encoded = match.groups()
    try:
        source_bytes = base64.b64decode(encoded, validate=True)
    except ValueError as error:
        raise ValueError("Organization icon image data is invalid") from error
    if len(source_bytes) > _MAX_IMAGE_BYTES:
        raise ValueError("Organization icon image exceeds 2 MiB")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(source_bytes), formats=list(_IMAGE_FORMATS.values())) as source:
                if source.format != _IMAGE_FORMATS[media_type]:
                    raise ValueError("Organization icon image type does not match its data URL")
                if getattr(source, "is_animated", False) or getattr(source, "n_frames", 1) != 1:
                    raise ValueError("Organization icon image cannot be animated")
                width, height = source.size
                if width * height > _MAX_IMAGE_PIXELS:
                    raise ValueError("Organization icon image exceeds 4 million pixels")
                source.load()
                pixels = source.convert("RGBA")
    except ValueError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        UnidentifiedImageError,
    ) as error:
        raise ValueError("Organization icon image data is invalid") from error
    normalized = Image.new("RGBA", pixels.size)
    normalized.paste(pixels)
    normalized.thumbnail((_MAX_ICON_EDGE, _MAX_ICON_EDGE), Image.Resampling.LANCZOS)
    output = BytesIO()
    normalized.save(output, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")


def _organization_response(organization: Organization) -> OrganizationResponse:
    icon = (
        OrganizationIcon(kind=organization.icon_kind, value=organization.icon_value)
        if organization.icon_kind is not None and organization.icon_value is not None
        else None
    )
    return OrganizationResponse(
        id=organization.id,
        name=organization.name,
        created_by_user_id=organization.created_by_user_id,
        icon=icon,
    )


def _membership_response(membership: Membership) -> MembershipResponse:
    return MembershipResponse(**membership.__dict__)
