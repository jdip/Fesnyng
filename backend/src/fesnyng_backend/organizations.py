"""Organization membership API with tenant-scoped authorization."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from fesnyng_backend import auth
from fesnyng_backend.control_store import Membership, Organization

router = APIRouter(prefix="/organizations", tags=["organizations"])


class OrganizationResponse(BaseModel):
    id: str
    name: str
    created_by_user_id: str


class CreateOrganizationRequest(BaseModel):
    name: str = Field(max_length=120)


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


def _organization_response(organization: Organization) -> OrganizationResponse:
    return OrganizationResponse(**organization.__dict__)


def _membership_response(membership: Membership) -> MembershipResponse:
    return MembershipResponse(**membership.__dict__)
