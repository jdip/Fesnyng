"""Personal, durable workspace presentation preferences."""

from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from fesnyng_backend import auth

router = APIRouter(
    prefix="/organizations/{organization_id}/workspace-preferences", tags=["workspace"]
)


class WorkspacePreferences(BaseModel):
    thread_list_page_size: int = Field(ge=1, le=100)


def _preferences(request: Request, organization_id: str, user_id: str) -> WorkspacePreferences:
    return WorkspacePreferences(
        thread_list_page_size=auth.get_store(request).thread_list_page_size(
            organization_id, user_id
        )
    )


@router.get("")
def get_preferences(request: Request, organization_id: UUID) -> WorkspacePreferences:
    organization = str(organization_id)
    user = auth.current_user(request)
    auth.require_member(request, organization)
    return _preferences(request, organization, user.id)


@router.put("")
def update_preferences(
    request: Request, organization_id: UUID, body: WorkspacePreferences
) -> WorkspacePreferences:
    organization = str(organization_id)
    user = auth.require_unsafe_request(request)
    auth.require_member(request, organization)
    auth.get_store(request).set_thread_list_page_size(
        organization, user.id, body.thread_list_page_size
    )
    return _preferences(request, organization, user.id)
