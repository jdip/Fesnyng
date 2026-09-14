"""Control-plane ownership of agent identity, placement and desired configuration."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from fesnyng_backend.agent_models import (
    AgentConfiguration,
    AgentCreate,
    AgentUpdate,
    CredentialProfileCreate,
    OrganizationPolicy,
    PolicyUpdate,
)

if TYPE_CHECKING:
    from fesnyng_backend.control_store import ControlPlaneStore

AGENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS organization_policies (
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    version INTEGER NOT NULL,
    configuration TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(id),
    PRIMARY KEY (organization_id, version)
);
CREATE TABLE IF NOT EXISTS hosts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    api_url TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS organization_hosts (
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    host_id TEXT NOT NULL REFERENCES hosts(id),
    PRIMARY KEY (organization_id, host_id)
);
CREATE TABLE IF NOT EXISTS organization_host_credentials (
    organization_id TEXT NOT NULL, host_id TEXT NOT NULL, token TEXT NOT NULL,
    PRIMARY KEY(organization_id,host_id),
    FOREIGN KEY(organization_id,host_id) REFERENCES organization_hosts(organization_id,host_id)
);
CREATE TABLE IF NOT EXISTS credential_profiles (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    name TEXT NOT NULL,
    provider TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(id),
    UNIQUE (organization_id, id)
);
CREATE TABLE IF NOT EXISTS departments (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    name TEXT NOT NULL,
    parent_id TEXT,
    head_agent_id TEXT,
    UNIQUE (organization_id, id),
    FOREIGN KEY (organization_id, parent_id)
        REFERENCES departments(organization_id, id),
    FOREIGN KEY (organization_id, head_agent_id)
        REFERENCES agents(organization_id, id)
);
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    name TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    host_id TEXT NOT NULL,
    reports_to_agent_id TEXT,
    department_id TEXT,
    desired_version INTEGER NOT NULL,
    applied_version INTEGER,
    UNIQUE (organization_id, id),
    FOREIGN KEY (organization_id, host_id)
        REFERENCES organization_hosts(organization_id, host_id),
    FOREIGN KEY (organization_id, reports_to_agent_id)
        REFERENCES agents(organization_id, id),
    FOREIGN KEY (organization_id, department_id)
        REFERENCES departments(organization_id, id)
);
CREATE TABLE IF NOT EXISTS workspace_preferences (
    organization_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    thread_list_page_size INTEGER NOT NULL CHECK(thread_list_page_size BETWEEN 1 AND 100),
    PRIMARY KEY (organization_id, user_id),
    FOREIGN KEY (organization_id, user_id)
        REFERENCES organization_memberships(organization_id, user_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS thread_pins (
    organization_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (organization_id, user_id, agent_id, session_id),
    FOREIGN KEY (organization_id, user_id)
        REFERENCES organization_memberships(organization_id, user_id) ON DELETE CASCADE,
    FOREIGN KEY (organization_id, agent_id)
        REFERENCES agents(organization_id, id)
);
CREATE TABLE IF NOT EXISTS thread_acknowledgements (
    organization_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    delivery_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('read', 'failure_handled')),
    outcome_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (organization_id, user_id, agent_id, session_id, delivery_id, kind, outcome_id),
    FOREIGN KEY (organization_id, user_id)
        REFERENCES organization_memberships(organization_id, user_id) ON DELETE CASCADE,
    FOREIGN KEY (organization_id, agent_id)
        REFERENCES agents(organization_id, id)
);
CREATE TABLE IF NOT EXISTS agent_configurations (
    agent_id TEXT NOT NULL REFERENCES agents(id),
    version INTEGER NOT NULL,
    configuration TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(id),
    PRIMARY KEY (agent_id, version)
);
"""


class ConfigurationConflict(ValueError):
    """The caller edited a superseded desired version."""


class AgentStore:
    def __init__(self, control: ControlPlaneStore):
        self.control = control

    def register_host(self, host_id: str, name: str, api_url: str, organization_id: str) -> None:
        """Installation authority allocates a host; never exposed to member HTTP calls."""
        with self.control.connect() as connection:
            connection.execute(
                "INSERT INTO hosts(id,name,api_url) VALUES(?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name,api_url=excluded.api_url",
                (host_id, name, api_url),
            )
            connection.execute(
                "INSERT OR IGNORE INTO organization_hosts(organization_id,host_id) VALUES(?,?)",
                (organization_id, host_id),
            )

    def set_host_credential(self, organization_id: str, host_id: str, token: str) -> None:
        if len(token) < 32:
            raise ValueError("Host binding token must contain at least 32 characters")
        with self.control.connect() as connection:
            connection.execute(
                "INSERT INTO organization_host_credentials VALUES(?,?,?) ON CONFLICT(organization_id,host_id) DO UPDATE SET token=excluded.token",
                (organization_id, host_id, token),
            )

    def host_connection(self, organization_id: str, host_id: str) -> tuple[str, str]:
        with self.control.connect() as connection:
            row = connection.execute(
                "SELECT h.api_url,c.token FROM hosts h JOIN organization_host_credentials c ON c.host_id=h.id WHERE c.organization_id=? AND h.id=?",
                (organization_id, host_id),
            ).fetchone()
        if row is None:
            raise LookupError("Host binding is not configured by installation")
        return row[0], row[1]

    def acknowledge_host(
        self, organization_id: str, agent_id: str, host_id: str, version: int
    ) -> None:
        with self.control.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT host_id FROM agents WHERE organization_id=? AND id=?",
                (organization_id, agent_id),
            ).fetchone()
            if row is None or row[0] != host_id:
                raise ValueError("Acknowledgement is not from the assigned host")
            if not connection.execute(
                "SELECT 1 FROM agent_configurations WHERE agent_id=? AND version=?",
                (agent_id, version),
            ).fetchone():
                raise ValueError("Acknowledged configuration version does not exist")
            connection.execute(
                "UPDATE agents SET applied_version=MAX(COALESCE(applied_version,0),?) WHERE organization_id=? AND id=?",
                (version, organization_id, agent_id),
            )

    def create_agent(
        self, organization_id: str, actor_id: str, values: dict[str, Any]
    ) -> dict[str, Any]:
        values = AgentCreate.model_validate(values).model_dump(mode="json")
        agent_id = str(uuid4())
        with self.control.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not connection.execute(
                "SELECT 1 FROM organization_hosts WHERE organization_id=? AND host_id=?",
                (organization_id, values["host_id"]),
            ).fetchone():
                raise ValueError("Host is not allocated to this organization")
            _validate_profile(connection, organization_id, values["configuration"])
            reporting = values.get("reports_to_agent_id")
            _validate_reporting(connection, organization_id, agent_id, reporting)
            department = values.get("department_id")
            _validate_department(connection, organization_id, department)
            connection.execute(
                "INSERT INTO agents(id,organization_id,name,title,host_id,reports_to_agent_id,department_id,desired_version) "
                "VALUES(?,?,?,?,?,?,?,1)",
                (
                    agent_id,
                    organization_id,
                    values["name"],
                    values.get("title", ""),
                    values["host_id"],
                    reporting,
                    department,
                ),
            )
            connection.execute(
                "INSERT INTO agent_configurations(agent_id,version,configuration,created_by) "
                "VALUES(?,1,?,?)",
                (agent_id, json.dumps(values.get("configuration", {})), actor_id),
            )
        return self.get_agent(organization_id, agent_id)

    def get_agent(self, organization_id: str, agent_id: str) -> dict[str, Any]:
        with self.control.connect() as connection:
            row = connection.execute(
                "SELECT a.*,c.configuration FROM agents a JOIN agent_configurations c "
                "ON c.agent_id=a.id AND c.version=a.desired_version "
                "WHERE a.organization_id=? AND a.id=?",
                (organization_id, agent_id),
            ).fetchone()
        if row is None:
            raise LookupError("Agent not found")
        result = dict(row)
        result["configuration"] = AgentConfiguration.model_validate_json(
            result["configuration"]
        ).model_dump(mode="json")
        result["configuration_status"] = (
            "applied" if result["applied_version"] == result["desired_version"] else "pending"
        )
        return result

    def update_agent(
        self, organization_id: str, agent_id: str, actor_id: str, values: dict[str, Any]
    ) -> dict[str, Any]:
        values = AgentUpdate.model_validate(values).model_dump(mode="json", exclude_unset=True)
        if any(values.get(key, "present") is None for key in ("name", "title", "configuration")):
            raise ValueError("Name, title and configuration cannot be null")
        with self.control.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT a.*,c.configuration FROM agents a JOIN agent_configurations c "
                "ON c.agent_id=a.id AND c.version=a.desired_version "
                "WHERE a.organization_id=? AND a.id=?",
                (organization_id, agent_id),
            ).fetchone()
            if current is None:
                raise LookupError("Agent not found")
            if values["expected_version"] != current["desired_version"]:
                raise ConfigurationConflict("Agent configuration version conflict")
            version = current["desired_version"] + 1
            configuration = values.get("configuration", json.loads(current["configuration"]))
            original_runtime = AgentConfiguration.model_validate_json(
                current["configuration"]
            ).runtime_type
            if AgentConfiguration.model_validate(configuration).runtime_type != original_runtime:
                raise ValueError("Use the dedicated harness switch operation to change a harness")
            _validate_profile(connection, organization_id, configuration)
            reporting = values.get("reports_to_agent_id", current["reports_to_agent_id"])
            _validate_reporting(connection, organization_id, agent_id, reporting)
            department = values.get("department_id", current["department_id"])
            _validate_department(connection, organization_id, department)
            connection.execute(
                "UPDATE agents SET name=?,title=?,reports_to_agent_id=?,department_id=?,desired_version=? WHERE id=?",
                (
                    values.get("name", current["name"]),
                    values.get("title", current["title"]),
                    reporting,
                    department,
                    version,
                    agent_id,
                ),
            )
            connection.execute(
                "INSERT INTO agent_configurations(agent_id,version,configuration,created_by) "
                "VALUES(?,?,?,?)",
                (agent_id, version, json.dumps(configuration), actor_id),
            )
        return self.get_agent(organization_id, agent_id)

    def create_profile(
        self, organization_id: str, actor_id: str, values: dict[str, Any]
    ) -> dict[str, str]:
        profile = CredentialProfileCreate.model_validate(values)
        profile_id = str(uuid4())
        with self.control.connect() as connection:
            connection.execute(
                "INSERT INTO credential_profiles(id,organization_id,name,provider,created_by) "
                "VALUES(?,?,?,?,?)",
                (profile_id, organization_id, profile.name, profile.provider, actor_id),
            )
        return {
            "id": profile_id,
            "organization_id": organization_id,
            "name": profile.name,
            "provider": profile.provider,
        }

    def get_policy(self, organization_id: str) -> dict[str, Any]:
        with self.control.connect() as connection:
            _ensure_policy(connection, organization_id)
            row = connection.execute(
                "SELECT version,configuration FROM organization_policies WHERE organization_id=? "
                "ORDER BY version DESC LIMIT 1",
                (organization_id,),
            ).fetchone()
        if row is None:
            raise LookupError("Organization not found")
        return {
            "organization_id": organization_id,
            "desired_version": row["version"],
            "configuration": json.loads(row["configuration"]),
        }

    def update_policy(
        self, organization_id: str, actor_id: str, values: dict[str, Any]
    ) -> dict[str, Any]:
        update = PolicyUpdate.model_validate(values)
        with self.control.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _ensure_policy(connection, organization_id)
            row = connection.execute(
                "SELECT MAX(version) FROM organization_policies WHERE organization_id=?",
                (organization_id,),
            ).fetchone()
            if row[0] != update.expected_version:
                raise ConfigurationConflict("Organization policy version conflict")
            connection.execute(
                "INSERT INTO organization_policies(organization_id,version,configuration,created_by) "
                "VALUES(?,?,?,?)",
                (
                    organization_id,
                    update.expected_version + 1,
                    update.configuration.model_dump_json(),
                    actor_id,
                ),
            )
        return self.get_policy(organization_id)

    def list_agents(self, organization_id: str) -> list[dict[str, Any]]:
        with self.control.connect() as connection:
            ids = connection.execute(
                "SELECT id FROM agents WHERE organization_id=? ORDER BY name,id",
                (organization_id,),
            ).fetchall()
        return [self.get_agent(organization_id, row["id"]) for row in ids]

    def list_hosts(self, organization_id: str) -> list[dict[str, str]]:
        with self.control.connect() as connection:
            rows = connection.execute(
                "SELECT hosts.id,hosts.name FROM hosts JOIN organization_hosts ON hosts.id=organization_hosts.host_id "
                "WHERE organization_hosts.organization_id=? ORDER BY hosts.name",
                (organization_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_profiles(self, organization_id: str) -> list[dict[str, str]]:
        with self.control.connect() as connection:
            rows = connection.execute(
                "SELECT id,organization_id,name,provider FROM credential_profiles "
                "WHERE organization_id=? ORDER BY name,id",
                (organization_id,),
            ).fetchall()
        return [dict(row) for row in rows]


def _validate_reporting(
    connection: sqlite3.Connection, organization_id: str, agent_id: str, reporting: str | None
) -> None:
    visited = {agent_id}
    while reporting is not None:
        if reporting in visited:
            raise ValueError("Reporting relationship would create a cycle")
        visited.add(reporting)
        row = connection.execute(
            "SELECT reports_to_agent_id FROM agents WHERE organization_id=? AND id=?",
            (organization_id, reporting),
        ).fetchone()
        if row is None:
            raise ValueError("Reporting agent not found in this organization")
        reporting = row[0]


def _validate_department(
    connection: sqlite3.Connection, organization_id: str, department_id: str | None
) -> None:
    if (
        department_id is not None
        and not connection.execute(
            "SELECT 1 FROM departments WHERE organization_id=? AND id=?",
            (organization_id, department_id),
        ).fetchone()
    ):
        raise ValueError("Department not found in this organization")


def _validate_profile(
    connection: sqlite3.Connection, organization_id: str, configuration: dict[str, Any]
) -> None:
    profile_id = configuration.get("profile_id")
    if (
        profile_id is not None
        and not connection.execute(
            "SELECT 1 FROM credential_profiles WHERE organization_id=? AND id=?",
            (organization_id, profile_id),
        ).fetchone()
    ):
        raise ValueError("Credential profile not found in this organization")


def _ensure_policy(connection: sqlite3.Connection, organization_id: str) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO organization_policies(organization_id,version,configuration,created_by) "
        "SELECT id,1,?,created_by_user_id FROM organizations WHERE id=?",
        (OrganizationPolicy().model_dump_json(), organization_id),
    )
