"""Durable host ownership, scoped trust and applied runtime configuration."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID, uuid4

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
                    desired_state TEXT NOT NULL DEFAULT 'running',
                    lifecycle_state TEXT NOT NULL DEFAULT 'pending',
                    switch_state TEXT CHECK(switch_state IN ('capturing','frozen')),
                    switch_source_version INTEGER,
                    switch_target_runtime TEXT CHECK(switch_target_runtime IN ('opencode','codex')),
                    UNIQUE(organization_id,agent_id)
                );
                CREATE TABLE IF NOT EXISTS host_lifecycle_confirmations (
                    code_digest TEXT PRIMARY KEY, organization_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL, actor_id TEXT NOT NULL, action TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    FOREIGN KEY(organization_id,agent_id)
                        REFERENCES host_agents(organization_id,agent_id)
                );
                CREATE TABLE IF NOT EXISTS host_sessions (
                    session_id TEXT PRIMARY KEY, organization_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL, directory TEXT NOT NULL, title TEXT NOT NULL,
                    runtime_type TEXT NOT NULL DEFAULT 'opencode'
                        CHECK(runtime_type IN ('opencode','codex')),
                    fesnyng_project_id TEXT,
                    project_provenance_initialized INTEGER NOT NULL DEFAULT 0
                        CHECK(project_provenance_initialized IN (0,1)),
                    deleted_at INTEGER,
                    archived_at INTEGER,
                    created_at INTEGER NOT NULL DEFAULT (unixepoch()),
                    FOREIGN KEY(organization_id,agent_id) REFERENCES host_agents(organization_id,agent_id)
                );
                CREATE TABLE IF NOT EXISTS host_thread_snapshots (
                    session_id TEXT PRIMARY KEY REFERENCES host_sessions(session_id),
                    organization_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    captured_at INTEGER NOT NULL DEFAULT (unixepoch()),
                    snapshot TEXT NOT NULL,
                    FOREIGN KEY(organization_id,agent_id) REFERENCES host_agents(organization_id,agent_id)
                );
                CREATE TABLE IF NOT EXISTS host_workspace_creations (
                    creation_id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    request_fingerprint TEXT NOT NULL,
                    directory TEXT NOT NULL,
                    project_id TEXT,
                    requested_checkout_branch TEXT,
                    repository_url TEXT,
                    checkout_branch TEXT,
                    starting_revision TEXT,
                    state TEXT NOT NULL CHECK(state IN ('reserved','native_attempted','completed','uncertain')),
                    native_receipt TEXT,
                    created_at INTEGER NOT NULL DEFAULT (unixepoch()),
                    updated_at INTEGER NOT NULL DEFAULT (unixepoch()),
                    FOREIGN KEY(organization_id,agent_id) REFERENCES host_agents(organization_id,agent_id)
                );
                CREATE TABLE IF NOT EXISTS host_workspace_bindings (
                    workspace_id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    session_id TEXT NOT NULL UNIQUE REFERENCES host_sessions(session_id),
                    directory TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('repository','ordinary','fork')),
                    creation_id TEXT,
                    state TEXT NOT NULL CHECK(state IN ('ready','removing','removed','replacing','unavailable')),
                    generation INTEGER NOT NULL DEFAULT 0,
                    safety_digest TEXT,
                    working_branch TEXT,
                    working_revision TEXT,
                    history_snapshot TEXT,
                    history_digest TEXT,
                    last_actor TEXT,
                    updated_at INTEGER NOT NULL DEFAULT (unixepoch()),
                    FOREIGN KEY(organization_id,agent_id) REFERENCES host_agents(organization_id,agent_id)
                );
            """)
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(host_sessions)")
            }
            if "deleted_at" not in columns:
                connection.execute("ALTER TABLE host_sessions ADD COLUMN deleted_at INTEGER")
            if "archived_at" not in columns:
                connection.execute("ALTER TABLE host_sessions ADD COLUMN archived_at INTEGER")
            if "runtime_type" not in columns:
                connection.execute(
                    "ALTER TABLE host_sessions ADD COLUMN runtime_type TEXT NOT NULL DEFAULT 'opencode'"
                )
            if "fesnyng_project_id" not in columns:
                connection.execute("ALTER TABLE host_sessions ADD COLUMN fesnyng_project_id TEXT")
            if "project_provenance_initialized" not in columns:
                connection.execute(
                    "ALTER TABLE host_sessions ADD COLUMN project_provenance_initialized INTEGER NOT NULL DEFAULT 0"
                )
            workspace_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(host_workspace_creations)")
            }
            if "project_id" not in workspace_columns:
                connection.execute(
                    "ALTER TABLE host_workspace_creations ADD COLUMN project_id TEXT"
                )
            if "requested_checkout_branch" not in workspace_columns:
                connection.execute(
                    "ALTER TABLE host_workspace_creations ADD COLUMN requested_checkout_branch TEXT"
                )
            binding_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(host_workspace_bindings)")
            }
            if "working_branch" not in binding_columns:
                connection.execute(
                    "ALTER TABLE host_workspace_bindings ADD COLUMN working_branch TEXT"
                )
            if "working_revision" not in binding_columns:
                connection.execute(
                    "ALTER TABLE host_workspace_bindings ADD COLUMN working_revision TEXT"
                )
            agent_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(host_agents)")
            }
            if "desired_state" not in agent_columns:
                connection.execute(
                    "ALTER TABLE host_agents ADD COLUMN desired_state TEXT NOT NULL DEFAULT 'running'"
                )
            if "lifecycle_state" not in agent_columns:
                connection.execute(
                    "ALTER TABLE host_agents ADD COLUMN lifecycle_state TEXT NOT NULL DEFAULT 'running'"
                )
            if "switch_state" not in agent_columns:
                connection.execute("ALTER TABLE host_agents ADD COLUMN switch_state TEXT")
            if "switch_source_version" not in agent_columns:
                connection.execute(
                    "ALTER TABLE host_agents ADD COLUMN switch_source_version INTEGER"
                )
            if "switch_target_runtime" not in agent_columns:
                connection.execute("ALTER TABLE host_agents ADD COLUMN switch_target_runtime TEXT")
            if "frozen_at" not in columns:
                connection.execute("ALTER TABLE host_sessions ADD COLUMN frozen_at INTEGER")
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
                if current["switch_state"] == "capturing":
                    raise ValueError("Harness switch capture is in progress")
                if current["switch_state"] == "frozen" and (
                    envelope.configuration.runtime_type != current["switch_target_runtime"]
                    or envelope.version <= current["switch_source_version"]
                ):
                    raise ValueError("Harness switch target configuration is required")
                if previous == envelope and (
                    current["applied_envelope"] is not None
                    and HostAgentConfiguration.model_validate_json(current["applied_envelope"])
                    == envelope
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
            "desired_state": agent["desired_state"],
            "lifecycle_state": agent["lifecycle_state"],
            "switch_state": agent["switch_state"],
            "switch_source_version": agent["switch_source_version"],
            "switch_target_runtime": agent["switch_target_runtime"],
            "error": agent["error"],
        }

    def mark_applied(self, envelope: HostAgentConfiguration) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT desired_envelope FROM host_agents WHERE agent_id=? AND organization_id=?",
                (str(envelope.agent_id), str(envelope.organization_id)),
            ).fetchone()
            if (
                current is None
                or HostAgentConfiguration.model_validate_json(current["desired_envelope"])
                != envelope
            ):
                raise ValueError("Configuration changed during runtime application")
            changed = connection.execute(
                """UPDATE host_agents
                SET desired_envelope=?,applied_envelope=?,runtime_state='running',
                    lifecycle_state=CASE WHEN desired_state='running' AND lifecycle_state NOT IN ('transitioning','recovering') THEN 'running' ELSE lifecycle_state END,
                    switch_state=CASE
                        WHEN switch_state='frozen' AND switch_target_runtime=? AND switch_source_version<?
                        THEN NULL ELSE switch_state END,
                    switch_source_version=CASE
                        WHEN switch_state='frozen' AND switch_target_runtime=? AND switch_source_version<?
                        THEN NULL ELSE switch_source_version END,
                    switch_target_runtime=CASE
                        WHEN switch_state='frozen' AND switch_target_runtime=? AND switch_source_version<?
                        THEN NULL ELSE switch_target_runtime END,
                    error=NULL WHERE agent_id=? AND organization_id=?""",
                (
                    envelope.model_dump_json(),
                    envelope.model_dump_json(),
                    envelope.configuration.runtime_type,
                    envelope.version,
                    envelope.configuration.runtime_type,
                    envelope.version,
                    envelope.configuration.runtime_type,
                    envelope.version,
                    str(envelope.agent_id),
                    str(envelope.organization_id),
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

    def set_lifecycle_state(
        self,
        organization_id: str,
        agent_id: str,
        *,
        desired: str | None = None,
        state: str,
        error: str | None = None,
    ) -> None:
        if desired is not None and desired not in {"running", "stopped"}:
            raise ValueError("Unknown desired lifecycle state")
        with self.connect() as connection:
            if desired is None:
                changed = connection.execute(
                    """UPDATE host_agents SET lifecycle_state=?,error=?
                    WHERE organization_id=? AND agent_id=? AND switch_state IS NULL""",
                    (state, error, organization_id, agent_id),
                ).rowcount
            else:
                changed = connection.execute(
                    """UPDATE host_agents SET desired_state=?,lifecycle_state=?,error=?
                    WHERE organization_id=? AND agent_id=? AND switch_state IS NULL""",
                    (desired, state, error, organization_id, agent_id),
                ).rowcount
            if changed != 1:
                row = connection.execute(
                    "SELECT switch_state FROM host_agents WHERE organization_id=? AND agent_id=?",
                    (organization_id, agent_id),
                ).fetchone()
                if row is None:
                    raise LookupError("Agent not found")
                raise ValueError("Harness switch is in progress")

    def save_lifecycle_confirmation(
        self,
        organization_id: str,
        agent_id: str,
        actor_id: str,
        action: str,
        digest: str,
        expires: int,
    ) -> None:
        self.agent(organization_id, agent_id)
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM host_lifecycle_confirmations WHERE expires_at<?", (expires - 300,)
            )
            connection.execute(
                "DELETE FROM host_lifecycle_confirmations WHERE organization_id=? AND agent_id=? AND actor_id=? AND action=?",
                (organization_id, agent_id, actor_id, action),
            )
            connection.execute(
                "INSERT INTO host_lifecycle_confirmations VALUES(?,?,?,?,?,?)",
                (digest, organization_id, agent_id, actor_id, action, expires),
            )

    def consume_lifecycle_confirmation(
        self, organization_id: str, agent_id: str, actor_id: str, action: str, digest: str, now: int
    ) -> bool:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT 1 FROM host_lifecycle_confirmations WHERE code_digest=? AND organization_id=? AND agent_id=? AND actor_id=? AND action=? AND expires_at>=?",
                (digest, organization_id, agent_id, actor_id, action, now),
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                "DELETE FROM host_lifecycle_confirmations WHERE code_digest=?", (digest,)
            )
        return True

    def save_session(
        self,
        organization_id: str,
        agent_id: str,
        session_id: str,
        directory: str,
        title: str,
        *,
        runtime_type: str = "opencode",
    ) -> None:
        if runtime_type not in {"opencode", "codex"}:
            raise ValueError("Unknown thread harness")
        self.agent(organization_id, agent_id)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT organization_id,agent_id,directory,title,runtime_type FROM host_sessions "
                "WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if current is not None:
                if current["runtime_type"] != runtime_type:
                    raise ValueError("Thread runtime provenance is immutable")
                if (
                    current["organization_id"],
                    current["agent_id"],
                    current["directory"],
                    current["title"],
                ) != (organization_id, agent_id, directory, title):
                    raise ValueError("Thread binding is immutable")
                return
            connection.execute(
                "INSERT INTO host_sessions(session_id,organization_id,agent_id,directory,title,runtime_type) "
                "VALUES(?,?,?,?,?,?)",
                (session_id, organization_id, agent_id, directory, title, runtime_type),
            )

    def reserve_workspace_creation(
        self,
        organization_id: str,
        agent_id: str,
        creation_id: str,
        request_fingerprint: str,
        directory: str,
        repository_url: str | None,
        checkout_branch: str | None,
        *,
        project_id: str | None = None,
        requested_checkout_branch: str | None = None,
    ) -> dict[str, Any]:
        """Reserve one exact native-create intent before any native call occurs."""
        UUID(creation_id)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.require_writable(organization_id, agent_id, connection=connection)
            existing = connection.execute(
                "SELECT * FROM host_workspace_creations WHERE creation_id=?", (creation_id,)
            ).fetchone()
            if existing is not None:
                row = dict(existing)
                if (
                    row["organization_id"],
                    row["agent_id"],
                    row["request_fingerprint"],
                    row["directory"],
                    row["project_id"],
                    row["requested_checkout_branch"],
                    row["repository_url"],
                    row["checkout_branch"],
                ) != (
                    organization_id,
                    agent_id,
                    request_fingerprint,
                    directory,
                    project_id,
                    requested_checkout_branch,
                    repository_url,
                    checkout_branch,
                ):
                    raise ValueError("Workspace creation retry does not match its original request")
                return row
            connection.execute(
                """INSERT INTO host_workspace_creations(
                    creation_id,organization_id,agent_id,request_fingerprint,directory,
                    project_id,requested_checkout_branch,repository_url,checkout_branch,state
                ) VALUES(?,?,?,?,?,?,?,?,?,'reserved')""",
                (
                    creation_id,
                    organization_id,
                    agent_id,
                    request_fingerprint,
                    directory,
                    project_id,
                    requested_checkout_branch,
                    repository_url,
                    checkout_branch,
                ),
            )
        return self.workspace_creation(organization_id, agent_id, creation_id)

    def workspace_creation(
        self, organization_id: str, agent_id: str, creation_id: str
    ) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT * FROM host_workspace_creations
                WHERE creation_id=? AND organization_id=? AND agent_id=?""",
                (creation_id, organization_id, agent_id),
            ).fetchone()
        if row is None:
            raise LookupError("Workspace creation not found")
        return dict(row)

    def mark_workspace_prepared(
        self,
        organization_id: str,
        agent_id: str,
        creation_id: str,
        starting_revision: str | None,
    ) -> None:
        with self.connect() as connection:
            changed = connection.execute(
                """UPDATE host_workspace_creations SET starting_revision=?,updated_at=unixepoch()
                WHERE creation_id=? AND organization_id=? AND agent_id=? AND state='reserved'""",
                (starting_revision, creation_id, organization_id, agent_id),
            ).rowcount
        if changed != 1:
            raise ValueError("Workspace creation is not available for preparation")

    def mark_workspace_native_attempted(
        self, organization_id: str, agent_id: str, creation_id: str
    ) -> None:
        with self.connect() as connection:
            changed = connection.execute(
                """UPDATE host_workspace_creations SET state='native_attempted',updated_at=unixepoch()
                WHERE creation_id=? AND organization_id=? AND agent_id=? AND state='reserved'""",
                (creation_id, organization_id, agent_id),
            ).rowcount
        if changed != 1:
            raise ValueError("Workspace creation is not available for native creation")

    def complete_workspace_creation(
        self,
        organization_id: str,
        agent_id: str,
        creation_id: str,
        receipt: dict[str, Any],
    ) -> None:
        encoded = json.dumps(receipt, separators=(",", ":"))
        session_id = receipt.get("id")
        directory = receipt.get("directory")
        if not isinstance(session_id, str) or not isinstance(directory, str):
            raise TypeError("Workspace creation receipt cannot be recorded")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """UPDATE host_workspace_creations SET state='completed',native_receipt=?,updated_at=unixepoch()
                WHERE creation_id=? AND organization_id=? AND agent_id=?
                  AND state IN ('native_attempted','uncertain')""",
                (encoded, creation_id, organization_id, agent_id),
            ).rowcount
            if changed != 1:
                raise ValueError("Workspace creation receipt cannot be recorded")
            creation = connection.execute(
                """SELECT repository_url,directory FROM host_workspace_creations
                WHERE creation_id=? AND organization_id=? AND agent_id=?""",
                (creation_id, organization_id, agent_id),
            ).fetchone()
            if creation is None or creation["directory"] != directory:
                raise ValueError("Workspace creation receipt cannot be recorded")
            session = connection.execute(
                """SELECT 1 FROM host_sessions
                WHERE organization_id=? AND agent_id=? AND session_id=? AND directory=?""",
                (organization_id, agent_id, session_id, directory),
            ).fetchone()
            if session is not None:
                connection.execute(
                    """INSERT INTO host_workspace_bindings(
                        workspace_id,organization_id,agent_id,session_id,directory,kind,creation_id,state
                    ) VALUES(?,?,?,?,?,?,?,'ready')
                    ON CONFLICT(session_id) DO NOTHING""",
                    (
                        creation_id,
                        organization_id,
                        agent_id,
                        session_id,
                        directory,
                        "repository" if creation["repository_url"] is not None else "ordinary",
                        creation_id,
                    ),
                )

    def workspace_binding(
        self, organization_id: str, agent_id: str, session_id: str
    ) -> dict[str, Any] | None:
        """Return one host-owned workspace, lazily binding completed #119 receipts."""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT * FROM host_workspace_bindings
                WHERE organization_id=? AND agent_id=? AND session_id=?""",
                (organization_id, agent_id, session_id),
            ).fetchone()
            if row is not None:
                return dict(row)
            session = connection.execute(
                """SELECT directory FROM host_sessions
                WHERE organization_id=? AND agent_id=? AND session_id=? AND deleted_at IS NULL""",
                (organization_id, agent_id, session_id),
            ).fetchone()
            if session is None:
                raise LookupError("Thread not found")
            creations = connection.execute(
                """SELECT * FROM host_workspace_creations
                WHERE organization_id=? AND agent_id=? AND directory=? AND state='completed'""",
                (organization_id, agent_id, session["directory"]),
            ).fetchall()
            matching = []
            for creation in creations:
                try:
                    native = json.loads(creation["native_receipt"])
                except (TypeError, json.JSONDecodeError):
                    continue
                if isinstance(native, dict) and native.get("id") == session_id:
                    matching.append(creation)
            if len(matching) != 1:
                return None
            creation = matching[0]
            connection.execute(
                """INSERT INTO host_workspace_bindings(
                    workspace_id,organization_id,agent_id,session_id,directory,kind,creation_id,state
                ) VALUES(?,?,?,?,?,?,?,'ready')""",
                (
                    creation["creation_id"],
                    organization_id,
                    agent_id,
                    session_id,
                    session["directory"],
                    "repository" if creation["repository_url"] is not None else "ordinary",
                    creation["creation_id"],
                ),
            )
            row = connection.execute(
                "SELECT * FROM host_workspace_bindings WHERE workspace_id=?",
                (creation["creation_id"],),
            ).fetchone()
            assert row is not None
            return dict(row)

    def register_workspace_fork(
        self,
        organization_id: str,
        agent_id: str,
        source_session_id: str,
        session_id: str,
        directory: str,
    ) -> None:
        """Bind only a fork descended from a currently host-owned workspace."""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            source = connection.execute(
                """SELECT directory,state,creation_id FROM host_workspace_bindings
                WHERE organization_id=? AND agent_id=? AND session_id=?""",
                (organization_id, agent_id, source_session_id),
            ).fetchone()
            if source is None or source["state"] != "ready":
                return
            if not directory.startswith(source["directory"].rstrip("/") + "-fork-"):
                raise ValueError(
                    "Fork workspace directory is not derived from its host-owned source"
                )
            connection.execute(
                """INSERT INTO host_workspace_bindings(
                    workspace_id,organization_id,agent_id,session_id,directory,kind,creation_id,state
                ) VALUES(?,?,?,?,?,'fork',?,'ready') ON CONFLICT(session_id) DO NOTHING""",
                (
                    str(uuid4()),
                    organization_id,
                    agent_id,
                    session_id,
                    directory,
                    source["creation_id"],
                ),
            )

    def begin_workspace_removal(
        self,
        organization_id: str,
        agent_id: str,
        workspace_id: str,
        generation: int,
        actor: str,
        snapshot: dict[str, Any],
        history_digest: str,
        safety_digest: str,
        working_branch: str | None = None,
        working_revision: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # Admission and the durable snapshot share the same SQLite transaction.
            # DispatchStore.enqueue takes this lock before admitting a message, so a
            # later enqueue observes `removing` and is refused rather than racing a
            # destructive filesystem operation.
            binding = connection.execute(
                """SELECT session_id FROM host_workspace_bindings
                WHERE workspace_id=? AND organization_id=? AND agent_id=?""",
                (workspace_id, organization_id, agent_id),
            ).fetchone()
            if binding is None:
                raise ValueError("Workspace lifecycle request is stale or not ready")
            unsettled = connection.execute(
                """SELECT 1 FROM host_dispatches
                WHERE organization_id=? AND agent_id=? AND session_id=?
                  AND state NOT IN ('completed','failed','contributed','cancelled') LIMIT 1""",
                (organization_id, agent_id, binding["session_id"]),
            ).fetchone()
            if unsettled is not None:
                raise ValueError(
                    "Native session action is waiting for durable delivery reconciliation"
                )
            changed = connection.execute(
                """UPDATE host_workspace_bindings
                SET state='removing',last_actor=?,history_snapshot=?,history_digest=?,safety_digest=?,
                    working_branch=?,working_revision=?,
                    updated_at=unixepoch()
                WHERE workspace_id=? AND organization_id=? AND agent_id=? AND generation=? AND state='ready'""",
                (
                    actor,
                    json.dumps(snapshot, separators=(",", ":")),
                    history_digest,
                    safety_digest,
                    working_branch,
                    working_revision,
                    workspace_id,
                    organization_id,
                    agent_id,
                    generation,
                ),
            ).rowcount
        if changed != 1:
            raise ValueError("Workspace lifecycle request is stale or not ready")

    def complete_workspace_removal(
        self,
        organization_id: str,
        agent_id: str,
        workspace_id: str,
    ) -> None:
        with self.connect() as connection:
            changed = connection.execute(
                """UPDATE host_workspace_bindings
                SET state='removed',generation=generation+1,updated_at=unixepoch()
                WHERE workspace_id=? AND organization_id=? AND agent_id=? AND state='removing'""",
                (workspace_id, organization_id, agent_id),
            ).rowcount
        if changed != 1:
            raise ValueError("Workspace removal receipt cannot be recorded")

    def begin_workspace_replacement(
        self, organization_id: str, agent_id: str, workspace_id: str, generation: int, actor: str
    ) -> None:
        with self.connect() as connection:
            changed = connection.execute(
                """UPDATE host_workspace_bindings SET state='replacing',last_actor=?,updated_at=unixepoch()
                WHERE workspace_id=? AND organization_id=? AND agent_id=? AND generation=? AND state='removed'""",
                (actor, workspace_id, organization_id, agent_id, generation),
            ).rowcount
        if changed != 1:
            raise ValueError("Workspace replacement request is stale or unavailable")

    def complete_workspace_replacement(
        self, organization_id: str, agent_id: str, workspace_id: str
    ) -> None:
        with self.connect() as connection:
            changed = connection.execute(
                """UPDATE host_workspace_bindings SET state='ready',generation=generation+1,
                safety_digest=NULL,updated_at=unixepoch()
                WHERE workspace_id=? AND organization_id=? AND agent_id=? AND state='replacing'""",
                (workspace_id, organization_id, agent_id),
            ).rowcount
        if changed != 1:
            raise ValueError("Workspace replacement receipt cannot be recorded")

    def mark_workspace_unavailable(
        self, organization_id: str, agent_id: str, workspace_id: str
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE host_workspace_bindings SET state='unavailable',updated_at=unixepoch()
                WHERE workspace_id=? AND organization_id=? AND agent_id=? AND state IN ('removing','replacing')""",
                (workspace_id, organization_id, agent_id),
            )

    def restore_workspace_ready(
        self, organization_id: str, agent_id: str, workspace_id: str
    ) -> None:
        """Reopen only a pre-delete failure whose directory was reverified present."""
        with self.connect() as connection:
            changed = connection.execute(
                """UPDATE host_workspace_bindings
                SET state='ready',safety_digest=NULL,working_branch=NULL,working_revision=NULL,
                    history_snapshot=NULL,history_digest=NULL,updated_at=unixepoch()
                WHERE workspace_id=? AND organization_id=? AND agent_id=? AND state='removing'""",
                (workspace_id, organization_id, agent_id),
            ).rowcount
        if changed != 1:
            raise ValueError("Workspace removal reconciliation is unavailable")

    def restore_workspace_removed(
        self, organization_id: str, agent_id: str, workspace_id: str
    ) -> None:
        """Reopen only a pre-replacement failure whose removed path still verifies absent."""
        with self.connect() as connection:
            changed = connection.execute(
                """UPDATE host_workspace_bindings SET state='removed',updated_at=unixepoch()
                WHERE workspace_id=? AND organization_id=? AND agent_id=? AND state='replacing'""",
                (workspace_id, organization_id, agent_id),
            ).rowcount
        if changed != 1:
            raise ValueError("Workspace replacement reconciliation is unavailable")

    def mark_workspace_creation_uncertain(
        self, organization_id: str, agent_id: str, creation_id: str
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE host_workspace_creations SET state='uncertain',updated_at=unixepoch()
                WHERE creation_id=? AND organization_id=? AND agent_id=?
                  AND state='native_attempted'""",
                (creation_id, organization_id, agent_id),
            )

    def begin_harness_switch(
        self, organization_id: str, agent_id: str, expected_version: int, target_runtime: str
    ) -> None:
        """Durably close admission before a caller captures old-harness history."""
        if target_runtime not in {"opencode", "codex"}:
            raise ValueError("Unknown target harness")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            agent = connection.execute(
                "SELECT * FROM host_agents WHERE organization_id=? AND agent_id=?",
                (organization_id, agent_id),
            ).fetchone()
            if agent is None:
                raise LookupError("Agent not found")
            desired = HostAgentConfiguration.model_validate_json(agent["desired_envelope"])
            applied = (
                HostAgentConfiguration.model_validate_json(agent["applied_envelope"])
                if agent["applied_envelope"]
                else None
            )
            if (
                desired.version != expected_version
                or applied != desired
                or desired.configuration.runtime_type == target_runtime
            ):
                raise ValueError("Harness switch source configuration is no longer current")
            if agent["switch_state"] is not None:
                if (
                    agent["switch_source_version"] == expected_version
                    and agent["switch_target_runtime"] == target_runtime
                ):
                    return
                raise ValueError("Another harness switch is already in progress")
            connection.execute(
                """UPDATE host_agents
                SET switch_state='capturing',switch_source_version=?,switch_target_runtime=?
                WHERE organization_id=? AND agent_id=?""",
                (expected_version, target_runtime, organization_id, agent_id),
            )

    def abort_harness_switch(self, organization_id: str, agent_id: str) -> None:
        """Re-open admission only before the permanent freeze commits."""
        with self.connect() as connection:
            connection.execute(
                """UPDATE host_agents
                SET switch_state=NULL,switch_source_version=NULL,switch_target_runtime=NULL
                WHERE organization_id=? AND agent_id=? AND switch_state='capturing'""",
                (organization_id, agent_id),
            )

    def harness_switch_frozen_for(
        self,
        organization_id: str,
        agent_id: str,
        source_version: int,
        target_runtime: str,
    ) -> bool:
        agent = self.agent(organization_id, agent_id)
        return (
            agent["switch_state"] == "frozen"
            and agent["switch_source_version"] == source_version
            and agent["switch_target_runtime"] == target_runtime
        )

    def commit_freeze(
        self, organization_id: str, agent_id: str, snapshots: dict[str, dict[str, Any]]
    ) -> None:
        """Persist verified snapshots and permanent thread freezes in one transaction."""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            agent = connection.execute(
                "SELECT switch_state FROM host_agents WHERE organization_id=? AND agent_id=?",
                (organization_id, agent_id),
            ).fetchone()
            if agent is None:
                raise LookupError("Agent not found")
            roots = connection.execute(
                """SELECT * FROM host_sessions
                WHERE organization_id=? AND agent_id=? AND deleted_at IS NULL
                ORDER BY session_id""",
                (organization_id, agent_id),
            ).fetchall()
            writable = [row for row in roots if row["frozen_at"] is None]
            if agent["switch_state"] == "frozen":
                if writable:
                    raise ValueError("Harness freeze state is inconsistent")
                return
            if agent["switch_state"] != "capturing":
                raise ValueError("Harness switch capture is not active")
            expected = {row["session_id"] for row in writable}
            if set(snapshots) != expected:
                raise ValueError("Harness snapshot set does not match writable threads")
            for row in writable:
                snapshot = snapshots[row["session_id"]]
                if (
                    snapshot.get("runtime_type") != row["runtime_type"]
                    or not isinstance(snapshot.get("session"), dict)
                    or snapshot["session"].get("id") != row["session_id"]
                ):
                    raise ValueError("Harness snapshot provenance is invalid")
                connection.execute(
                    """INSERT INTO host_thread_snapshots(session_id,organization_id,agent_id,snapshot)
                    VALUES(?,?,?,?)""",
                    (
                        row["session_id"],
                        organization_id,
                        agent_id,
                        json.dumps(snapshot, separators=(",", ":")),
                    ),
                )
            connection.execute(
                """UPDATE host_sessions SET frozen_at=unixepoch()
                WHERE organization_id=? AND agent_id=? AND deleted_at IS NULL AND frozen_at IS NULL""",
                (organization_id, agent_id),
            )
            connection.execute(
                """UPDATE host_agents SET switch_state='frozen'
                WHERE organization_id=? AND agent_id=?""",
                (organization_id, agent_id),
            )

    def frozen_snapshot(
        self, organization_id: str, agent_id: str, session_id: str
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT snapshot FROM host_thread_snapshots
                WHERE organization_id=? AND agent_id=? AND session_id=?""",
                (organization_id, agent_id, session_id),
            ).fetchone()
        return json.loads(row["snapshot"]) if row is not None else None

    def require_writable(
        self,
        organization_id: str,
        agent_id: str,
        session_id: str | None = None,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        """Reject writes during capture and forever for frozen thread provenance."""
        if connection is None:
            with self.connect() as owned:
                self.require_writable(organization_id, agent_id, session_id, connection=owned)
            return
        if session_id is not None:
            session = connection.execute(
                """SELECT frozen_at FROM host_sessions
                WHERE organization_id=? AND agent_id=? AND session_id=? AND deleted_at IS NULL""",
                (organization_id, agent_id, session_id),
            ).fetchone()
            if session is None:
                raise LookupError("Thread not found")
            if session["frozen_at"] is not None:
                raise ValueError("Thread is permanently frozen and read-only")
        agent = connection.execute(
            "SELECT switch_state FROM host_agents WHERE organization_id=? AND agent_id=?",
            (organization_id, agent_id),
        ).fetchone()
        if agent is None:
            raise LookupError("Agent not found")
        if agent["switch_state"] is not None:
            raise ValueError("Harness switch is in progress; new work is blocked")

    def session(self, organization_id: str, agent_id: str, session_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT * FROM host_sessions
                WHERE organization_id=? AND agent_id=? AND session_id=? AND deleted_at IS NULL""",
                (organization_id, agent_id, session_id),
            ).fetchone()
        if row is None:
            raise LookupError("Thread not found")
        return dict(row)

    def sessions(
        self, organization_id: str, agent_id: str, *, archived: bool | None = None
    ) -> list[dict[str, Any]]:
        self.agent(organization_id, agent_id)
        query = (
            """SELECT * FROM host_sessions
            WHERE organization_id=? AND agent_id=? AND deleted_at IS NULL
              AND archived_at IS NOT NULL
            ORDER BY created_at,session_id"""
            if archived is True
            else """SELECT * FROM host_sessions
            WHERE organization_id=? AND agent_id=? AND deleted_at IS NULL
              AND archived_at IS NULL
            ORDER BY created_at,session_id"""
            if archived is False
            else """SELECT * FROM host_sessions
            WHERE organization_id=? AND agent_id=? AND deleted_at IS NULL
            ORDER BY created_at,session_id"""
        )
        with self.connect() as connection:
            rows = connection.execute(
                query,
                (organization_id, agent_id),
            ).fetchall()
        sessions = [dict(row) for row in rows]
        for session in sessions:
            session.pop("project_provenance_initialized", None)
        return sessions

    def set_session_project_provenance(
        self, organization_id: str, agent_id: str, session_id: str, project_id: str | None
    ) -> None:
        """Record the control-plane grouping known for one host-owned thread."""
        with self.connect() as connection:
            changed = connection.execute(
                """UPDATE host_sessions
                SET fesnyng_project_id=?, project_provenance_initialized=1
                WHERE organization_id=? AND agent_id=? AND session_id=? AND deleted_at IS NULL""",
                (project_id, organization_id, agent_id, session_id),
            ).rowcount
        if changed != 1:
            raise LookupError("Thread not found")

    def inherit_session_project_provenance(
        self, organization_id: str, agent_id: str, session_id: str, project_id: str | None
    ) -> None:
        """Attach delegated-thread provenance once without changing an existing grouping."""
        if project_id is None:
            return
        with self.connect() as connection:
            changed = connection.execute(
                """UPDATE host_sessions
                SET fesnyng_project_id=?, project_provenance_initialized=1
                WHERE organization_id=? AND agent_id=? AND session_id=? AND deleted_at IS NULL
                  AND project_provenance_initialized=0""",
                (project_id, organization_id, agent_id, session_id),
            ).rowcount
        if changed != 1:
            self.session(organization_id, agent_id, session_id)

    def rename_session(
        self, organization_id: str, agent_id: str, session_id: str, title: str
    ) -> None:
        with self.connect() as connection:
            changed = connection.execute(
                """UPDATE host_sessions SET title=?
                WHERE organization_id=? AND agent_id=? AND session_id=? AND deleted_at IS NULL""",
                (title, organization_id, agent_id, session_id),
            ).rowcount
        if changed != 1:
            raise LookupError("Thread not found")

    def delete_session(self, organization_id: str, agent_id: str, session_id: str) -> None:
        """Hide a deleted native session without destroying durable receipts."""
        with self.connect() as connection:
            changed = connection.execute(
                """UPDATE host_sessions SET deleted_at=unixepoch()
                WHERE organization_id=? AND agent_id=? AND session_id=? AND deleted_at IS NULL""",
                (organization_id, agent_id, session_id),
            ).rowcount
        if changed != 1:
            raise LookupError("Thread not found")

    def archive_session(
        self, organization_id: str, agent_id: str, session_id: str, archived_at: int | None
    ) -> None:
        with self.connect() as connection:
            changed = connection.execute(
                """UPDATE host_sessions SET archived_at=?
                WHERE organization_id=? AND agent_id=? AND session_id=? AND deleted_at IS NULL""",
                (archived_at, organization_id, agent_id, session_id),
            ).rowcount
        if changed != 1:
            raise LookupError("Thread not found")
