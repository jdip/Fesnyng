"""Versioned control-plane handoff and authorized native session contracts."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StringConstraints

from fesnyng_backend.agent_models import (
    AgentConfiguration,
    Contract,
    Name,
    OrganizationPolicy,
    PermissionRule,
    Slug,
)

NativeID = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,160}$")]


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


class HarnessSwitch(Contract):
    expected_version: int = Field(ge=1)
    target_runtime_type: Literal["opencode", "codex"]


class Actor(Contract):
    kind: Literal["human", "agent"]
    id: UUID
    name: Name
    session_id: str | None = Field(default=None, max_length=160, pattern=r"^[A-Za-z0-9_-]+$")


def permission_rules(
    envelope: HostAgentConfiguration, overrides: list[PermissionRule] | None = None
) -> list[dict[str, str]]:
    """A complete native suffix replaces earlier matches; mandatory rules always win."""
    rules = [{"permission": "*", "pattern": "*", "action": envelope.policy.default_permission}]
    if envelope.policy.allow_thread_overrides:
        rules.extend(rule.model_dump() for rule in overrides or [])
    rules.extend(rule.model_dump() for rule in envelope.policy.mandatory_permissions)
    # Pinned OpenCode does not inherit ask rules or refresh reused child policies.
    # Keep broad native delegation; restricted threads cannot cross that boundary.
    if any(rule["action"] != "allow" for rule in rules):
        rules.append({"permission": "task", "pattern": "*", "action": "deny"})
    return rules
