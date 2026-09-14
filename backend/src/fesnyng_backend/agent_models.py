"""Validated desired-state contracts shared by control-plane APIs and hosts."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NativeSkill(Contract):
    name: Slug
    content: str = Field(min_length=1, max_length=200_000)
    explicit_only: bool = False


class PermissionRule(Contract):
    permission: str = Field(min_length=1, max_length=100)
    pattern: str = Field(default="*", min_length=1, max_length=2000)
    action: Literal["allow", "ask", "deny"]


class AgentConfiguration(Contract):
    execution_type: Literal["docker"] = "docker"
    runtime_type: Literal["opencode", "codex"] = "opencode"
    provider: Literal["openai"] = "openai"
    model: str = Field(default="gpt-6-astra", min_length=1, max_length=120)
    profile_id: UUID | None = None
    instructions: str = Field(default="", max_length=200_000)
    workspace: Slug = "default"
    skills: list[NativeSkill] = Field(default_factory=list, max_length=100)


class AgentCreate(Contract):
    name: Name
    title: str = Field(default="", max_length=120)
    host_id: UUID
    reports_to_agent_id: UUID | None = None
    department_id: UUID | None = None
    configuration: AgentConfiguration = Field(default_factory=AgentConfiguration)


class AgentUpdate(Contract):
    expected_version: int = Field(ge=1)
    name: Name | None = None
    title: str | None = Field(default=None, max_length=120)
    reports_to_agent_id: UUID | None = None
    department_id: UUID | None = None
    configuration: AgentConfiguration | None = None


class HarnessSwitchRequest(Contract):
    expected_version: int = Field(ge=1)
    target_runtime_type: Literal["opencode", "codex"]


class CredentialProfileCreate(Contract):
    name: Name
    provider: Literal["openai"] = "openai"


class OrganizationPolicy(Contract):
    default_permission: Literal["allow", "ask", "deny"] = "allow"
    mandatory_permissions: list[PermissionRule] = Field(default_factory=list, max_length=100)
    allow_thread_overrides: bool = True


class PolicyUpdate(Contract):
    expected_version: int = Field(ge=1)
    configuration: OrganizationPolicy


class AgentResponse(Contract):
    id: UUID
    organization_id: UUID
    name: str
    title: str
    host_id: UUID
    reports_to_agent_id: UUID | None
    department_id: UUID | None
    desired_version: int
    applied_version: int | None
    configuration_status: Literal["pending", "applied"]
    configuration: AgentConfiguration


class DepartmentCreate(Contract):
    name: Name
    parent_id: UUID | None = None
    head_agent_id: UUID | None = None


class DepartmentUpdate(Contract):
    name: Name | None = None
    parent_id: UUID | None = None
    head_agent_id: UUID | None = None


class DepartmentResponse(Contract):
    id: UUID
    organization_id: UUID
    name: str
    parent_id: UUID | None
    head_agent_id: UUID | None


class ProfileResponse(CredentialProfileCreate):
    id: UUID
    organization_id: UUID


class HostResponse(Contract):
    id: UUID
    name: str


class PolicyResponse(Contract):
    organization_id: UUID
    desired_version: int
    configuration: OrganizationPolicy
