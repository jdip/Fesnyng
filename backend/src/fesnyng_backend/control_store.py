"""Control-plane-owned SQLite state for identities and organizations."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import BoundedSemaphore
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from fesnyng_backend.agent_storage import AGENT_SCHEMA

CONTROL_PLANE_SCHEMA_VERSION = 5
PASSWORD_MAX_BYTES = 256
PASSWORD_MIN_BYTES = 12
SCRYPT_N = 2**17
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_MAXMEM = 160 * 1024 * 1024
_PASSWORD_HASH_SLOTS = BoundedSemaphore(2)
LOGIN_MAX_FAILURES = 5
LOGIN_BLOCK_SECONDS = 60
_LOGIN_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{2,63}")


def _migrate_agent_configuration_provenance(connection: sqlite3.Connection) -> None:
    """Allow agent-authored desired versions without rewriting human history."""
    columns = {
        row["name"]: row for row in connection.execute("PRAGMA table_info(agent_configurations)")
    }
    if "created_by_agent" in columns and not columns["created_by"]["notnull"]:
        return
    connection.execute(
        """CREATE TABLE agent_configurations_migrating (
        agent_id TEXT NOT NULL REFERENCES agents(id),
        version INTEGER NOT NULL,
        configuration TEXT NOT NULL,
        created_by TEXT REFERENCES users(id),
        created_by_agent TEXT REFERENCES agents(id),
        CHECK ((created_by IS NULL) != (created_by_agent IS NULL)),
        PRIMARY KEY (agent_id, version)
        )"""
    )
    connection.execute(
        """INSERT INTO agent_configurations_migrating(agent_id,version,configuration,created_by)
        SELECT agent_id,version,configuration,created_by FROM agent_configurations"""
    )
    connection.execute("DROP TABLE agent_configurations")
    connection.execute("ALTER TABLE agent_configurations_migrating RENAME TO agent_configurations")


class BootstrapAlreadyComplete(RuntimeError):
    """Raised when an installation already has an owner bootstrap record."""


@dataclass(frozen=True)
class User:
    id: str
    login: str
    display_name: str


@dataclass(frozen=True)
class Membership:
    user_id: str
    organization_id: str
    role: str


@dataclass(frozen=True)
class Organization:
    id: str
    name: str
    created_by_user_id: str
    icon_kind: Literal["emoji", "image"] | None = None
    icon_value: str | None = None


@dataclass(frozen=True)
class Project:
    """Organization-owned navigation grouping for employee-native threads."""

    id: str
    organization_id: str
    name: str
    description: str | None
    target_repository_url: str | None
    default_checkout_branch: str | None
    archived: bool


@dataclass(frozen=True)
class SessionCredentials:
    token: str
    csrf_token: str
    expires_at: int


class ControlPlaneStore:
    """Own the control plane's small, durable SQLite schema."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a foreign-key-enforcing transaction that always closes."""

        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        """Create the control-plane-local schema without changing host schema state."""

        with self.connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS control_plane_schema (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    schema_version INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO control_plane_schema (singleton, schema_version)
                VALUES (1, ?)
                """,
                (CONTROL_PLANE_SCHEMA_VERSION,),
            )
            schema_version = connection.execute(
                "SELECT schema_version FROM control_plane_schema WHERE singleton = 1"
            ).fetchone()[0]
            if schema_version not in {1, 2, 3, 4, CONTROL_PLANE_SCHEMA_VERSION}:
                raise RuntimeError(
                    "Unsupported control-plane schema version "
                    f"{schema_version}; expected {CONTROL_PLANE_SCHEMA_VERSION}."
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    login TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS organizations (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    created_by_user_id TEXT NOT NULL REFERENCES users(id),
                    created_at INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS organization_memberships (
                    organization_id TEXT NOT NULL REFERENCES organizations(id),
                    user_id TEXT NOT NULL REFERENCES users(id),
                    role TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'member')),
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY (organization_id, user_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS organization_branding (
                    organization_id TEXT PRIMARY KEY REFERENCES organizations(id) ON DELETE CASCADE,
                    icon_kind TEXT NOT NULL CHECK (icon_kind IN ('emoji', 'image')),
                    icon_value TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL REFERENCES organizations(id),
                    name TEXT NOT NULL,
                    description TEXT,
                    target_repository_url TEXT,
                    default_checkout_branch TEXT,
                    archived_at INTEGER,
                    created_at INTEGER NOT NULL,
                    CHECK (
                        (target_repository_url IS NULL AND default_checkout_branch IS NULL)
                        OR (target_repository_url IS NOT NULL AND default_checkout_branch IS NOT NULL)
                    )
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS thread_projects (
                    organization_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
                    explicit_choice INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY (organization_id, agent_id, session_id),
                    FOREIGN KEY (organization_id, agent_id)
                        REFERENCES agents(organization_id, id)
                )
                """
            )
            thread_project_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(thread_projects)")
            }
            if "explicit_choice" not in thread_project_columns:
                connection.execute(
                    "ALTER TABLE thread_projects ADD COLUMN explicit_choice INTEGER NOT NULL DEFAULT 0"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    token_digest TEXT PRIMARY KEY,
                    csrf_digest TEXT NOT NULL,
                    user_id TEXT NOT NULL REFERENCES users(id),
                    expires_at INTEGER NOT NULL,
                    revoked_at INTEGER,
                    created_at INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS login_attempts (
                    login_digest TEXT PRIMARY KEY,
                    failed_count INTEGER NOT NULL,
                    blocked_until INTEGER NOT NULL
                )
                """
            )
            for statement in AGENT_SCHEMA.split(";"):
                if statement.strip():
                    connection.execute(statement)
            _migrate_agent_configuration_provenance(connection)
            agent_columns = {row["name"] for row in connection.execute("PRAGMA table_info(agents)")}
            if "department_id" not in agent_columns:
                connection.execute("ALTER TABLE agents ADD COLUMN department_id TEXT")
            if schema_version != CONTROL_PLANE_SCHEMA_VERSION:
                connection.execute(
                    "UPDATE control_plane_schema SET schema_version=? WHERE singleton=1",
                    (CONTROL_PLANE_SCHEMA_VERSION,),
                )

    def bootstrap_owner(self, login: str, display_name: str, password: str) -> User:
        """Create the offline installation owner once, never from an HTTP request."""

        normalized_login = _normalize_login(login)
        normalized_display_name = _normalize_display_name(display_name)
        password_hash = _hash_password(password)
        user = User(id=str(uuid4()), login=normalized_login, display_name=normalized_display_name)

        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT EXISTS(SELECT 1 FROM users)").fetchone()[0]:
                raise BootstrapAlreadyComplete("An owner bootstrap already exists.")
            connection.execute(
                """
                INSERT INTO users (id, login, display_name, password_hash, created_at)
                VALUES (?, ?, ?, ?, unixepoch())
                """,
                (user.id, user.login, user.display_name, password_hash),
            )
        return user

    def create_organization(self, owner_id: str, name: str) -> Organization:
        """Create an organization and its founding owner membership together."""

        organization = Organization(
            id=str(uuid4()), name=_normalize_organization_name(name), created_by_user_id=owner_id
        )
        with self.connect() as connection:
            if not connection.execute("SELECT 1 FROM users WHERE id = ?", (owner_id,)).fetchone():
                raise LookupError("Organization creator does not exist.")
            connection.execute(
                """
                INSERT INTO organizations (id, name, created_by_user_id, created_at)
                VALUES (?, ?, ?, unixepoch())
                """,
                (organization.id, organization.name, organization.created_by_user_id),
            )
            connection.execute(
                """
                INSERT INTO organization_memberships (organization_id, user_id, role, created_at)
                VALUES (?, ?, 'owner', unixepoch())
                """,
                (organization.id, owner_id),
            )
        return organization

    def membership_for(self, user_id: str, organization_id: str) -> Membership:
        """Return one membership or raise when that user cannot access the organization."""

        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT user_id, organization_id, role
                FROM organization_memberships
                WHERE user_id = ? AND organization_id = ?
                """,
                (user_id, organization_id),
            ).fetchone()
        if row is None:
            raise LookupError("Organization membership does not exist.")
        return Membership(
            user_id=row["user_id"], organization_id=row["organization_id"], role=row["role"]
        )

    def create_project(self, organization_id: str, values: dict[str, str | None]) -> Project:
        project = Project(
            id=str(uuid4()),
            organization_id=organization_id,
            name=_normalize_project_name(values["name"]),
            description=_normalize_project_description(values.get("description")),
            target_repository_url=_normalize_repository_url(values.get("target_repository_url")),
            default_checkout_branch=_normalize_checkout_branch(
                values.get("default_checkout_branch")
            ),
            archived=False,
        )
        _validate_project_repository(project.target_repository_url, project.default_checkout_branch)
        with self.connect() as connection:
            if not connection.execute(
                "SELECT 1 FROM organizations WHERE id=?", (organization_id,)
            ).fetchone():
                raise LookupError("Organization not found.")
            connection.execute(
                """INSERT INTO projects(
                id,organization_id,name,description,target_repository_url,default_checkout_branch,created_at
                ) VALUES(?,?,?,?,?,?,unixepoch())""",
                (
                    project.id,
                    project.organization_id,
                    project.name,
                    project.description,
                    project.target_repository_url,
                    project.default_checkout_branch,
                ),
            )
        return project

    def list_projects(self, organization_id: str, include_archived: bool = False) -> list[Project]:
        where = (
            "organization_id=?" if include_archived else "organization_id=? AND archived_at IS NULL"
        )
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM projects WHERE {where} ORDER BY name,id", (organization_id,)
            ).fetchall()
        return [_project_from_row(row) for row in rows]

    def project_for(self, organization_id: str, project_id: str) -> Project:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE organization_id=? AND id=?",
                (organization_id, project_id),
            ).fetchone()
        if row is None:
            raise LookupError("Project not found.")
        return _project_from_row(row)

    def update_project(
        self, organization_id: str, project_id: str, values: dict[str, str | None]
    ) -> Project:
        current = self.project_for(organization_id, project_id)
        project = Project(
            id=current.id,
            organization_id=current.organization_id,
            name=_normalize_project_name(values.get("name", current.name)),
            description=_normalize_project_description(
                values.get("description", current.description)
            ),
            target_repository_url=_normalize_repository_url(
                values.get("target_repository_url", current.target_repository_url)
            ),
            default_checkout_branch=_normalize_checkout_branch(
                values.get("default_checkout_branch", current.default_checkout_branch)
            ),
            archived=current.archived,
        )
        _validate_project_repository(project.target_repository_url, project.default_checkout_branch)
        with self.connect() as connection:
            changed = connection.execute(
                """UPDATE projects SET name=?,description=?,target_repository_url=?,
                default_checkout_branch=? WHERE organization_id=? AND id=?""",
                (
                    project.name,
                    project.description,
                    project.target_repository_url,
                    project.default_checkout_branch,
                    organization_id,
                    project_id,
                ),
            ).rowcount
        if not changed:
            raise LookupError("Project not found.")
        return project

    def set_project_archived(
        self, organization_id: str, project_id: str, archived: bool
    ) -> Project:
        with self.connect() as connection:
            changed = connection.execute(
                """UPDATE projects SET archived_at=CASE WHEN ? THEN unixepoch() ELSE NULL END
                WHERE organization_id=? AND id=?""",
                (archived, organization_id, project_id),
            ).rowcount
        if not changed:
            raise LookupError("Project not found.")
        return self.project_for(organization_id, project_id)

    def delete_project(self, organization_id: str, project_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE thread_projects SET project_id=NULL,explicit_choice=1
                WHERE organization_id=? AND project_id=?""",
                (organization_id, project_id),
            )
            changed = connection.execute(
                "DELETE FROM projects WHERE organization_id=? AND id=?",
                (organization_id, project_id),
            ).rowcount
        if not changed:
            raise LookupError("Project not found.")

    def project_id_for_thread(
        self, organization_id: str, agent_id: str, session_id: str
    ) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT project_id FROM thread_projects
                WHERE organization_id=? AND agent_id=? AND session_id=?""",
                (organization_id, agent_id, session_id),
            ).fetchone()
        return str(row["project_id"]) if row is not None and row["project_id"] is not None else None

    def list_thread_projects(self, organization_id: str, agent_id: str) -> list[dict[str, str]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT session_id,project_id FROM thread_projects
                WHERE organization_id=? AND agent_id=? AND project_id IS NOT NULL ORDER BY session_id""",
                (organization_id, agent_id),
            ).fetchall()
        return [{"session_id": row["session_id"], "project_id": row["project_id"]} for row in rows]

    def set_thread_project(
        self, organization_id: str, agent_id: str, session_id: str, project_id: str | None
    ) -> str | None:
        with self.connect() as connection:
            if not connection.execute(
                "SELECT 1 FROM agents WHERE organization_id=? AND id=?", (organization_id, agent_id)
            ).fetchone():
                raise LookupError("Agent not found")
            if project_id is None:
                connection.execute(
                    """INSERT INTO thread_projects(
                    organization_id,agent_id,session_id,project_id,explicit_choice,created_at
                    ) VALUES(?,?,?,?,1,unixepoch())
                    ON CONFLICT(organization_id,agent_id,session_id)
                    DO UPDATE SET project_id=NULL,explicit_choice=1""",
                    (organization_id, agent_id, session_id, None),
                )
                return None
            self._active_project(connection, organization_id, project_id)
            connection.execute(
                """INSERT INTO thread_projects(
                organization_id,agent_id,session_id,project_id,explicit_choice,created_at
                ) VALUES(?,?,?,?,1,unixepoch())
                ON CONFLICT(organization_id,agent_id,session_id)
                DO UPDATE SET project_id=excluded.project_id,explicit_choice=1""",
                (organization_id, agent_id, session_id, project_id),
            )
        return project_id

    def attach_project_if_ungrouped(
        self, organization_id: str, agent_id: str, session_id: str, project_id: str
    ) -> str | None:
        """Attach recovered host provenance once without replacing a human choice."""

        with self.connect() as connection:
            if not connection.execute(
                "SELECT 1 FROM agents WHERE organization_id=? AND id=?", (organization_id, agent_id)
            ).fetchone():
                raise LookupError("Agent not found")
            existing = connection.execute(
                """SELECT project_id FROM thread_projects
                WHERE organization_id=? AND agent_id=? AND session_id=?""",
                (organization_id, agent_id, session_id),
            ).fetchone()
            if existing is not None:
                return str(existing["project_id"]) if existing["project_id"] is not None else None
            self._existing_project(connection, organization_id, project_id)
            connection.execute(
                """INSERT INTO thread_projects(
                organization_id,agent_id,session_id,project_id,explicit_choice,created_at
                ) VALUES(?,?,?,?,0,unixepoch())""",
                (organization_id, agent_id, session_id, project_id),
            )
            row = connection.execute(
                """SELECT project_id FROM thread_projects
                WHERE organization_id=? AND agent_id=? AND session_id=?""",
                (organization_id, agent_id, session_id),
            ).fetchone()
        return str(row["project_id"]) if row is not None else None

    @staticmethod
    def _active_project(
        connection: sqlite3.Connection, organization_id: str, project_id: str
    ) -> None:
        if not connection.execute(
            "SELECT 1 FROM projects WHERE organization_id=? AND id=? AND archived_at IS NULL",
            (organization_id, project_id),
        ).fetchone():
            raise ValueError("Project is not available for new thread grouping.")

    @staticmethod
    def _existing_project(
        connection: sqlite3.Connection, organization_id: str, project_id: str
    ) -> None:
        if not connection.execute(
            "SELECT 1 FROM projects WHERE organization_id=? AND id=?",
            (organization_id, project_id),
        ).fetchone():
            raise ValueError("Project is not available for thread grouping.")

    def thread_list_page_size(self, organization_id: str, user_id: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT thread_list_page_size FROM workspace_preferences
                WHERE organization_id=? AND user_id=?""",
                (organization_id, user_id),
            ).fetchone()
        return int(row["thread_list_page_size"]) if row is not None else 6

    def set_thread_list_page_size(
        self, organization_id: str, user_id: str, thread_list_page_size: int
    ) -> int:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO workspace_preferences(
                organization_id,user_id,thread_list_page_size
                ) VALUES(?,?,?)
                ON CONFLICT(organization_id,user_id) DO UPDATE SET
                thread_list_page_size=excluded.thread_list_page_size""",
                (organization_id, user_id, thread_list_page_size),
            )
        return self.thread_list_page_size(organization_id, user_id)

    def list_thread_pins(self, organization_id: str, user_id: str, agent_id: str) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT session_id FROM thread_pins
                WHERE organization_id=? AND user_id=? AND agent_id=?
                ORDER BY created_at,session_id""",
                (organization_id, user_id, agent_id),
            ).fetchall()
        return [row["session_id"] for row in rows]

    def pin_thread(
        self, organization_id: str, user_id: str, agent_id: str, session_id: str
    ) -> list[str]:
        with self.connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO thread_pins(
                organization_id,user_id,agent_id,session_id,created_at
                ) VALUES(?,?,?,?,unixepoch())""",
                (organization_id, user_id, agent_id, session_id),
            )
        return self.list_thread_pins(organization_id, user_id, agent_id)

    def unpin_thread(
        self, organization_id: str, user_id: str, agent_id: str, session_id: str
    ) -> list[str]:
        with self.connect() as connection:
            connection.execute(
                """DELETE FROM thread_pins
                WHERE organization_id=? AND user_id=? AND agent_id=? AND session_id=?""",
                (organization_id, user_id, agent_id, session_id),
            )
        return self.list_thread_pins(organization_id, user_id, agent_id)

    def list_thread_acknowledgements(
        self, organization_id: str, user_id: str, agent_id: str
    ) -> list[dict[str, str]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT session_id,delivery_id,kind,outcome_id
                FROM thread_acknowledgements
                WHERE organization_id=? AND user_id=? AND agent_id=?
                ORDER BY created_at,session_id,delivery_id,kind,outcome_id""",
                (organization_id, user_id, agent_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def acknowledge_thread(
        self,
        organization_id: str,
        user_id: str,
        agent_id: str,
        session_id: str,
        delivery_id: str,
        kind: str,
        outcome_id: str,
    ) -> list[dict[str, str]]:
        with self.connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO thread_acknowledgements(
                organization_id,user_id,agent_id,session_id,delivery_id,kind,outcome_id,created_at
                ) VALUES(?,?,?,?,?,?,?,unixepoch())""",
                (organization_id, user_id, agent_id, session_id, delivery_id, kind, outcome_id),
            )
        return self.list_thread_acknowledgements(organization_id, user_id, agent_id)

    def login(
        self, login: str, password: str, lifetime_seconds: int
    ) -> tuple[User, SessionCredentials] | None:
        """Return a matching user without disclosing whether the login exists."""

        try:
            normalized_login = _normalize_login(login)
        except ValueError:
            verify_password(password, _DUMMY_HASH)
            return None
        login_digest = _digest(normalized_login)
        now = int(time.time())
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id, login, display_name, password_hash FROM users WHERE login = ?",
                (normalized_login,),
            ).fetchone()
            attempt = connection.execute(
                "SELECT failed_count, blocked_until FROM login_attempts WHERE login_digest = ?",
                (login_digest,),
            ).fetchone()
            blocked = attempt is not None and attempt["blocked_until"] > now
        if blocked:
            return None
        verified = verify_password(
            password, row["password_hash"] if row is not None else _DUMMY_HASH
        )
        if row is None or not verified:
            with self.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                previous = connection.execute(
                    "SELECT failed_count FROM login_attempts WHERE login_digest = ?",
                    (login_digest,),
                ).fetchone()
                failures = (previous["failed_count"] if previous else 0) + 1
                blocked_until = now + LOGIN_BLOCK_SECONDS if failures >= LOGIN_MAX_FAILURES else 0
                connection.execute(
                    """INSERT INTO login_attempts (login_digest, failed_count, blocked_until) VALUES (?, ?, ?)
                    ON CONFLICT(login_digest) DO UPDATE SET failed_count = excluded.failed_count, blocked_until = excluded.blocked_until""",
                    (login_digest, failures, blocked_until),
                )
            return None
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT password_hash FROM users WHERE id = ?", (row["id"],)
            ).fetchone()
            if current is None or current["password_hash"] != row["password_hash"]:
                return None
            connection.execute("DELETE FROM login_attempts WHERE login_digest = ?", (login_digest,))
            session = _issue_session(connection, row["id"], lifetime_seconds)
        return User(id=row["id"], login=row["login"], display_name=row["display_name"]), session

    def user_for_session(self, token: str) -> User | None:
        """Read active session identity directly from durable revocation state."""

        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT users.id, users.login, users.display_name
                FROM sessions JOIN users ON users.id = sessions.user_id
                WHERE sessions.token_digest = ?
                  AND sessions.revoked_at IS NULL
                  AND sessions.expires_at > unixepoch()
                """,
                (_digest(token),),
            ).fetchone()
        if row is None:
            return None
        return User(id=row["id"], login=row["login"], display_name=row["display_name"])

    def session_matches_csrf(self, token: str, csrf_token: str) -> bool:
        """Validate a per-session synchronizer token for unsafe browser requests."""

        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT csrf_digest FROM sessions
                WHERE token_digest = ? AND revoked_at IS NULL AND expires_at > unixepoch()
                """,
                (_digest(token),),
            ).fetchone()
        return row is not None and hmac.compare_digest(row["csrf_digest"], _digest(csrf_token))

    def csrf_for_session(self, token: str) -> str | None:
        return _csrf_token(token) if self.user_for_session(token) is not None else None

    def revoke_session(self, token: str) -> None:
        """Make a browser session unusable immediately."""

        with self.connect() as connection:
            connection.execute(
                "UPDATE sessions SET revoked_at = unixepoch() WHERE token_digest = ?",
                (_digest(token),),
            )

    def list_memberships(self, organization_id: str) -> list[tuple[Membership, User]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT organization_memberships.user_id, organization_memberships.organization_id,
                organization_memberships.role, users.login, users.display_name FROM organization_memberships
                JOIN users ON users.id = organization_memberships.user_id WHERE organization_id = ?""",
                (organization_id,),
            ).fetchall()
        return [
            (
                Membership(row["user_id"], row["organization_id"], row["role"]),
                User(row["user_id"], row["login"], row["display_name"]),
            )
            for row in rows
        ]

    def list_organizations(self, user_id: str) -> list[Organization]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT organizations.id, organizations.name, organizations.created_by_user_id,
                organization_branding.icon_kind, organization_branding.icon_value
                FROM organizations JOIN organization_memberships
                ON organization_memberships.organization_id = organizations.id
                LEFT JOIN organization_branding ON organization_branding.organization_id = organizations.id
                WHERE organization_memberships.user_id = ? ORDER BY organizations.name""",
                (user_id,),
            ).fetchall()
        return [_organization_from_row(row) for row in rows]

    def organization_for_member(self, user_id: str, organization_id: str) -> Organization:
        self.membership_for(user_id, organization_id)
        with self.connect() as connection:
            row = connection.execute(
                """SELECT organizations.id, organizations.name, organizations.created_by_user_id,
                organization_branding.icon_kind, organization_branding.icon_value
                FROM organizations LEFT JOIN organization_branding
                ON organization_branding.organization_id = organizations.id WHERE organizations.id = ?""",
                (organization_id,),
            ).fetchone()
        if row is None:
            raise LookupError("Organization does not exist.")
        return _organization_from_row(row)

    def set_organization_icon(
        self, organization_id: str, icon_kind: str | None, icon_value: str | None
    ) -> None:
        with self.connect() as connection:
            if icon_kind is None:
                connection.execute(
                    "DELETE FROM organization_branding WHERE organization_id = ?",
                    (organization_id,),
                )
            else:
                connection.execute(
                    """INSERT INTO organization_branding(organization_id,icon_kind,icon_value)
                    VALUES(?,?,?) ON CONFLICT(organization_id) DO UPDATE SET
                    icon_kind=excluded.icon_kind,icon_value=excluded.icon_value""",
                    (organization_id, icon_kind, icon_value),
                )

    def add_member(
        self,
        organization_id: str,
        login: str,
        display_name: str,
        password: str | None,
        role: str,
        *,
        actor_id: str,
    ) -> Membership:
        if role not in {"owner", "admin", "member"}:
            raise ValueError("Unsupported organization role.")
        login = _normalize_login(login)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            manager = _membership_manager(connection, organization_id, actor_id)
            if role == "owner" and manager != "owner":
                raise PermissionError("Only an owner can grant the owner role.")
            row = connection.execute("SELECT id FROM users WHERE login = ?", (login,)).fetchone()
            if row is None:
                if password is None:
                    raise ValueError("A password is required for a new user.")
                user_id = str(uuid4())
                connection.execute(
                    "INSERT INTO users (id, login, display_name, password_hash, created_at) VALUES (?, ?, ?, ?, unixepoch())",
                    (
                        user_id,
                        login,
                        _normalize_display_name(display_name),
                        _hash_password(password),
                    ),
                )
            else:
                if password is not None:
                    raise ValueError("Existing user credentials cannot be overwritten.")
                user_id = row["id"]
            existing = connection.execute(
                "SELECT role FROM organization_memberships WHERE organization_id = ? AND user_id = ?",
                (organization_id, user_id),
            ).fetchone()
            if existing is not None and existing["role"] == "owner" and manager != "owner":
                raise PermissionError("Only an owner can change an owner membership.")
            if existing is not None and existing["role"] == "owner" and role != "owner":
                owners = connection.execute(
                    "SELECT COUNT(*) FROM organization_memberships WHERE organization_id = ? AND role = 'owner'",
                    (organization_id,),
                ).fetchone()[0]
                if owners <= 1:
                    raise ValueError("An organization must retain an owner.")
            connection.execute(
                """INSERT INTO organization_memberships (organization_id, user_id, role, created_at)
                VALUES (?, ?, ?, unixepoch())
                ON CONFLICT(organization_id, user_id) DO UPDATE SET role = excluded.role""",
                (organization_id, user_id, role),
            )
        return Membership(user_id=user_id, organization_id=organization_id, role=role)

    def remove_member(self, organization_id: str, user_id: str, *, actor_id: str) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            manager = _membership_manager(connection, organization_id, actor_id)
            row = connection.execute(
                "SELECT role FROM organization_memberships WHERE organization_id = ? AND user_id = ?",
                (organization_id, user_id),
            ).fetchone()
            if row is None:
                raise LookupError("Organization membership does not exist.")
            if row["role"] == "owner" and manager != "owner":
                raise PermissionError("Only an owner can remove an owner membership.")
            if row["role"] == "owner":
                owners = connection.execute(
                    "SELECT COUNT(*) FROM organization_memberships WHERE organization_id = ? AND role = 'owner'",
                    (organization_id,),
                ).fetchone()[0]
                if owners <= 1:
                    raise ValueError("An organization must retain an owner.")
            connection.execute(
                "DELETE FROM organization_memberships WHERE organization_id = ? AND user_id = ?",
                (organization_id, user_id),
            )

    def change_password(
        self, user_id: str, current_password: str, new_password: str, lifetime_seconds: int
    ) -> SessionCredentials | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT password_hash FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        if row is None or not verify_password(current_password, row["password_hash"]):
            return None
        replacement_hash = _hash_password(new_password)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE users SET password_hash = ? WHERE id = ? AND password_hash = ?",
                (replacement_hash, user_id, row["password_hash"]),
            ).rowcount
            if not changed:
                return None
            connection.execute(
                "UPDATE sessions SET revoked_at = unixepoch() WHERE user_id = ?", (user_id,)
            )
            return _issue_session(connection, user_id, lifetime_seconds)


def _membership_manager(connection: sqlite3.Connection, organization_id: str, actor_id: str) -> str:
    row = connection.execute(
        "SELECT role FROM organization_memberships WHERE organization_id = ? AND user_id = ?",
        (organization_id, actor_id),
    ).fetchone()
    if row is None:
        raise LookupError("Organization not found.")
    if row["role"] not in {"owner", "admin"}:
        raise PermissionError("Organization management is not permitted.")
    return row["role"]


def _issue_session(
    connection: sqlite3.Connection, user_id: str, lifetime_seconds: int
) -> SessionCredentials:
    token = secrets.token_urlsafe(32)
    csrf_token = _csrf_token(token)
    expires_at = int(time.time()) + lifetime_seconds
    connection.execute(
        """INSERT INTO sessions (token_digest, csrf_digest, user_id, expires_at, created_at)
        VALUES (?, ?, ?, ?, unixepoch())""",
        (_digest(token), _digest(csrf_token), user_id, expires_at),
    )
    return SessionCredentials(token=token, csrf_token=csrf_token, expires_at=expires_at)


def _organization_from_row(row: sqlite3.Row) -> Organization:
    return Organization(
        id=row["id"],
        name=row["name"],
        created_by_user_id=row["created_by_user_id"],
        icon_kind=row["icon_kind"],
        icon_value=row["icon_value"],
    )


def _project_from_row(row: sqlite3.Row) -> Project:
    return Project(
        id=row["id"],
        organization_id=row["organization_id"],
        name=row["name"],
        description=row["description"],
        target_repository_url=row["target_repository_url"],
        default_checkout_branch=row["default_checkout_branch"],
        archived=row["archived_at"] is not None,
    )


def _normalize_login(login: str) -> str:
    normalized = login.strip().lower()
    if not _LOGIN_PATTERN.fullmatch(normalized):
        raise ValueError(
            "Login must contain 3-64 lowercase letters, digits, dots, underscores, or hyphens."
        )
    return normalized


def _normalize_display_name(display_name: str) -> str:
    normalized = display_name.strip()
    if not 1 <= len(normalized) <= 100:
        raise ValueError("Display name must contain 1-100 characters.")
    return normalized


def _normalize_organization_name(name: str) -> str:
    normalized = name.strip()
    if not 1 <= len(normalized) <= 120:
        raise ValueError("Organization name must contain 1-120 characters.")
    return normalized


def _normalize_project_name(name: str | None) -> str:
    normalized = (name or "").strip()
    if not 1 <= len(normalized) <= 120:
        raise ValueError("Project name must contain 1-120 characters.")
    return normalized


def _normalize_project_description(description: str | None) -> str | None:
    if description is None:
        return None
    normalized = description.strip()
    if not normalized:
        return None
    if len(normalized) > 4_000:
        raise ValueError("Project description must contain at most 4000 characters.")
    return normalized


def _normalize_repository_url(repository_url: str | None) -> str | None:
    if repository_url is None:
        return None
    normalized = repository_url.strip()
    if not normalized:
        return None
    if len(normalized) > 2_000:
        raise ValueError("Project repository URL must contain at most 2000 characters.")
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"https", "http", "ssh"} or not parsed.hostname:
        raise ValueError("Project repository URL must use an HTTP(S) or SSH repository origin.")
    if (
        parsed.password is not None
        or (parsed.scheme in {"https", "http"} and parsed.username is not None)
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Project repository URL must not contain credentials, queries, or fragments."
        )
    return normalized


def _normalize_checkout_branch(branch: str | None) -> str | None:
    if branch is None:
        return None
    normalized = branch.strip()
    if not normalized:
        return None
    if len(normalized) > 500:
        raise ValueError("Project default checkout branch must contain at most 500 characters.")
    return normalized


def _validate_project_repository(repository_url: str | None, branch: str | None) -> None:
    if (repository_url is None) != (branch is None):
        raise ValueError(
            "A Project repository requires an explicit default checkout branch, and vice versa."
        )


def _hash_password(password: str) -> str:
    encoded = password.encode("utf-8")
    if not PASSWORD_MIN_BYTES <= len(encoded) <= PASSWORD_MAX_BYTES:
        raise ValueError(
            f"Password must contain {PASSWORD_MIN_BYTES}-{PASSWORD_MAX_BYTES} UTF-8 bytes."
        )
    salt = secrets.token_bytes(16)
    with _PASSWORD_HASH_SLOTS:
        digest = hashlib.scrypt(
            encoded, salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, maxmem=SCRYPT_MAXMEM
        )
    return "$".join(
        (
            "scrypt-v1",
            str(SCRYPT_N),
            str(SCRYPT_R),
            str(SCRYPT_P),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        )
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _csrf_token(token: str) -> str:
    return hashlib.sha256(f"fesnyng-csrf-v1:{token}".encode()).hexdigest()


def verify_password(password: str, password_hash: str) -> bool:
    """Validate a bounded password against a versioned scrypt record."""

    encoded = password.encode("utf-8")
    if len(encoded) > PASSWORD_MAX_BYTES:
        return False
    try:
        version, n, r, p, encoded_salt, expected = password_hash.split("$")
        if version != "scrypt-v1":
            return False
        salt = base64.urlsafe_b64decode(encoded_salt)
        expected_digest = base64.urlsafe_b64decode(expected)
        with _PASSWORD_HASH_SLOTS:
            actual = hashlib.scrypt(
                encoded, salt=salt, n=int(n), r=int(r), p=int(p), maxmem=SCRYPT_MAXMEM
            )
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(actual, expected_digest)


_DUMMY_HASH = _hash_password("dummy-password-for-login-timing")
