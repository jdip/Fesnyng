"""Versioned control-plane handoff and authorized native session contracts."""

from uuid import UUID

from pydantic import Field

from fesnyng_backend.agent_models import (
    AgentConfiguration,
    Contract,
    Name,
    OrganizationPolicy,
    Slug,
)


class HostAgentConfiguration(Contract):
    host_id: UUID
    organization_id: UUID
    agent_id: UUID
    version: int = Field(ge=1)
    name: Name
    title: str = Field(default="", max_length=120)
    reports_to_agent_id: UUID | None = None
    configuration: AgentConfiguration = Field(default_factory=AgentConfiguration)
    policy_version: int = Field(default=1, ge=1)
    policy: OrganizationPolicy = Field(default_factory=OrganizationPolicy)


class SessionCreate(Contract):
    title: Name = "New thread"
    workspace: Slug = "default"
