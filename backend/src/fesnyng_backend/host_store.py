"""Durable host ownership, scoped trust and applied runtime configuration."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

from fesnyng_backend.host_models import HostAgentConfiguration
from fesnyng_backend.settings import ServiceSettings
from fesnyng_backend.storage import initialize_service_state


class HostStore:
    def __init__(self, settings: ServiceSettings):
        self.settings = settings
        self.instance_id = initialize_service_state(settings).instance_id

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.settings.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS host_schema (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO host_schema VALUES(1,1);
                CREATE TABLE IF NOT EXISTS host_bindings (
                    organization_id TEXT PRIMARY KEY, token_digest TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS host_agents (
                    agent_id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL REFERENCES host_bindings(organization_id),
                    desired_envelope TEXT NOT NULL, applied_envelope TEXT,
                    runtime_password TEXT NOT NULL, agent_token TEXT NOT NULL,
                    runtime_state TEXT NOT NULL DEFAULT 'pending', error TEXT,
                    snapshot_image TEXT,
                    UNIQUE(organization_id,agent_id)
                );
                CREATE TABLE IF NOT EXISTS host_sessions (
                    session_id TEXT PRIMARY KEY, organization_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL, directory TEXT NOT NULL, title TEXT NOT NULL,
                    created_at INTEGER NOT NULL DEFAULT (unixepoch()),
                    FOREIGN KEY(organization_id,agent_id) REFERENCES host_agents(organization_id,agent_id)
                );
            """)
            if connection.execute("SELECT version FROM host_schema").fetchone()[0] != 1:
                raise RuntimeError("Unsupported host schema version")

    def bind_organization(self, organization_id: str, token: str) -> None:
        organization_id = str(UUID(organization_id))
        if len(token) < 32:
            raise ValueError("Organization binding token must contain at least 32 characters")
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO host_bindings VALUES(?,?) ON CONFLICT(organization_id) "
                "DO UPDATE SET token_digest=excluded.token_digest",
                (organization_id, hashlib.sha256(token.encode()).hexdigest()),
            )

    def authenticate(self, token: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT organization_id FROM host_bindings WHERE token_digest=?",
                (hashlib.sha256(token.encode()).hexdigest(),),
            ).fetchone()
        return row[0] if row else None

    def authenticate_agent(self, token: str) -> dict[str, str] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT organization_id,agent_id,desired_envelope FROM host_agents WHERE agent_token=?",
                (token,),
            ).fetchone()
        if row is None:
            return None
        return {
            "organization_id": row["organization_id"],
            "agent_id": row["agent_id"],
            "name": json.loads(row["desired_envelope"])["name"],
        }

    def stage_agent(self, envelope: HostAgentConfiguration) -> bool:
        if envelope.host_id != self.instance_id:
            raise ValueError("Configuration belongs to another host")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not connection.execute(
                "SELECT 1 FROM host_bindings WHERE organization_id=?",
                (str(envelope.organization_id),),
            ).fetchone():
                raise LookupError("Organization not bound to host")
            current = connection.execute(
                "SELECT * FROM host_agents WHERE agent_id=?", (str(envelope.agent_id),)
            ).fetchone()
            if current:
                previous = HostAgentConfiguration.model_validate_json(current["desired_envelope"])
                if previous.organization_id != envelope.organization_id:
                    raise ValueError("Agent belongs to another organization")
                if (
                    envelope.version < previous.version
                    or envelope.policy_version < previous.policy_version
                ):
                    raise ValueError("Configuration version conflict")
                if envelope.version == previous.version and envelope.model_dump(
                    exclude={"policy", "policy_version"}
                ) != previous.model_dump(exclude={"policy", "policy_version"}):
                    raise ValueError("Configuration version conflict")
                if (
                    envelope.policy_version == previous.policy_version
                    and envelope.policy != previous.policy
                ):
                    raise ValueError("Policy version conflict")
                if (
                    previous == envelope
                    and current["applied_envelope"] == envelope.model_dump_json()
                ):
                    return False
                connection.execute(
                    "UPDATE host_agents SET desired_envelope=?,runtime_state='pending',error=NULL WHERE agent_id=?",
                    (envelope.model_dump_json(), str(envelope.agent_id)),
                )
            else:
                connection.execute(
                    "INSERT INTO host_agents(agent_id,organization_id,desired_envelope,runtime_password,agent_token) VALUES(?,?,?,?,?)",
                    (
                        str(envelope.agent_id),
                        str(envelope.organization_id),
                        envelope.model_dump_json(),
                        secrets.token_urlsafe(32),
                        secrets.token_urlsafe(32),
                    ),
                )
        return True

    def agent(self, organization_id: str, agent_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM host_agents WHERE organization_id=? AND agent_id=?",
                (organization_id, agent_id),
            ).fetchone()
        if row is None:
            raise LookupError("Agent not found")
        return dict(row)

    def agent_status(self, organization_id: str, agent_id: str) -> dict[str, Any]:
        agent = self.agent(organization_id, agent_id)
        desired = json.loads(agent["desired_envelope"])
        applied = json.loads(agent["applied_envelope"]) if agent["applied_envelope"] else None
        return {
            "agent_id": agent_id,
            "organization_id": organization_id,
            "host_id": str(self.instance_id),
            "desired_version": desired["version"],
            "applied_version": applied["version"] if applied else None,
            "applied_policy_version": applied["policy_version"] if applied else None,
            "runtime_state": agent["runtime_state"],
            "error": agent["error"],
        }

    def mark_applied(self, envelope: HostAgentConfiguration) -> None:
        with self.connect() as connection:
            changed = connection.execute(
                "UPDATE host_agents SET applied_envelope=?,runtime_state='running',error=NULL WHERE agent_id=? AND organization_id=? AND desired_envelope=?",
                (
                    envelope.model_dump_json(),
                    str(envelope.agent_id),
                    str(envelope.organization_id),
                    envelope.model_dump_json(),
                ),
            ).rowcount
            if not changed:
                raise ValueError("Configuration changed during runtime application")

    def set_runtime_state(
        self, organization_id: str, agent_id: str, state: str, error: str | None = None
    ) -> None:
        self.agent(organization_id, agent_id)
        with self.connect() as connection:
            connection.execute(
                "UPDATE host_agents SET runtime_state=?,error=? WHERE organization_id=? AND agent_id=?",
                (state, error, organization_id, agent_id),
            )

    def save_session(
        self, organization_id: str, agent_id: str, session_id: str, directory: str, title: str
    ) -> None:
        self.agent(organization_id, agent_id)
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO host_sessions(session_id,organization_id,agent_id,directory,title) VALUES(?,?,?,?,?)",
                (session_id, organization_id, agent_id, directory, title),
            )

    def session(self, organization_id: str, agent_id: str, session_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM host_sessions WHERE organization_id=? AND agent_id=? AND session_id=?",
                (organization_id, agent_id, session_id),
            ).fetchone()
        if row is None:
            raise LookupError("Thread not found")
        return dict(row)

    def sessions(self, organization_id: str, agent_id: str) -> list[dict[str, Any]]:
        self.agent(organization_id, agent_id)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM host_sessions WHERE organization_id=? AND agent_id=? ORDER BY created_at,session_id",
                (organization_id, agent_id),
            ).fetchall()
        return [dict(row) for row in rows]
