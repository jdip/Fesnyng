"""Host-local OAuth credential ownership for assigned agent runtimes."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import os
import sqlite3
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx

OPENAI_AUTH_ISSUER = "https://auth.openai.com"
OPENAI_CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_READY = "ready"
_REFRESHING = "refreshing"
_REFRESH_UNCERTAIN = "refresh_uncertain"


class CredentialStore:
    """Durable, host-local profile and agent-assignment state."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        _prepare_private_database_file(self.database_path)
        connection = sqlite3.connect(self.database_path)
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
        """Create the host-private state schema without importing control-plane state."""
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS credential_profiles (
                    organization_id TEXT NOT NULL,
                    profile_id TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    account_id TEXT,
                    access TEXT NOT NULL DEFAULT '',
                    refresh TEXT NOT NULL DEFAULT '',
                    expires REAL NOT NULL DEFAULT 0,
                    residency TEXT,
                    generation INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL DEFAULT 'login_required',
                    operation TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (organization_id, profile_id)
                );
                CREATE TABLE IF NOT EXISTS agent_credential_bindings (
                    organization_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    profile_id TEXT NOT NULL,
                    key_digest TEXT NOT NULL UNIQUE,
                    PRIMARY KEY (organization_id, agent_id),
                    FOREIGN KEY (organization_id, profile_id)
                        REFERENCES credential_profiles(organization_id, profile_id)
                );
                """
            )

    def ensure_profile(self, organization_id: str, profile_id: str, name: str) -> None:
        """Register an organization-scoped profile without allowing it to move tenants."""
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT organization_id FROM credential_profiles WHERE profile_id=?", (profile_id,)
            ).fetchone()
            if existing is not None:
                if existing["organization_id"] != organization_id:
                    raise ValueError("Credential profile belongs to another organization")
                return
            connection.execute(
                "INSERT INTO credential_profiles(organization_id,profile_id,name) VALUES(?,?,?)",
                (organization_id, profile_id, name),
            )

    def assign_agent(self, organization_id: str, agent_id: str, profile_id: str, key: str) -> None:
        """Bind one host-issued agent key to one profile in the same organization."""
        key_digest = _digest(key)
        with self.connect() as connection:
            if not connection.execute(
                "SELECT 1 FROM credential_profiles WHERE organization_id=? AND profile_id=?",
                (organization_id, profile_id),
            ).fetchone():
                raise ValueError("Credential profile is not available to this organization")
            connection.execute(
                "INSERT INTO agent_credential_bindings(organization_id,agent_id,profile_id,key_digest) "
                "VALUES(?,?,?,?) ON CONFLICT(organization_id,agent_id) DO UPDATE SET "
                "profile_id=excluded.profile_id,key_digest=excluded.key_digest",
                (organization_id, agent_id, profile_id, key_digest),
            )

    def unassign_agent(self, organization_id: str, agent_id: str) -> None:
        """Revoke a host agent's profile access without changing that profile."""
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM agent_credential_bindings WHERE organization_id=? AND agent_id=?",
                (organization_id, agent_id),
            )

    def acquire_operation(
        self,
        organization_id: str,
        profile_id: str,
        allowed_states: Sequence[str],
        new_state: str,
        expected_generation: int | None = None,
    ) -> str | None:
        """Atomically reserve a profile transition and return its durable operation id."""
        if not allowed_states:
            raise ValueError("Credential operation requires an allowed state")
        operation = uuid.uuid4().hex
        placeholders = ",".join("?" for _ in allowed_states)
        generation_condition = ""
        parameters: tuple[object, ...] = (
            new_state,
            operation,
            organization_id,
            profile_id,
            *allowed_states,
        )
        if expected_generation is not None:
            generation_condition = " AND generation=?"
            parameters += (expected_generation,)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            tables = {
                row["name"]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "host_maintenance_admission" in tables:
                admission = connection.execute(
                    "SELECT state FROM host_maintenance_admission WHERE singleton=1"
                ).fetchone()
                if admission is None or admission["state"] != "open":
                    return None
            updated = connection.execute(
                "UPDATE credential_profiles SET state=?,operation=? "
                f"WHERE organization_id=? AND profile_id=? AND state IN ({placeholders}){generation_condition}",
                parameters,
            ).rowcount
        return operation if updated == 1 else None

    def save_tokens(
        self,
        organization_id: str,
        profile_id: str,
        tokens: Mapping[str, object],
        operation: str,
    ) -> None:
        """Commit an authenticated token set only for its owning operation and account."""
        try:
            access, account_id, residency, expires_in = _token_metadata(tokens)
        except (TypeError, ValueError) as error:
            self._fail_operation(organization_id, profile_id, operation)
            raise PermissionError("Credential claims are invalid") from error

        refresh_token = tokens.get("refresh_token")
        if refresh_token is not None and not isinstance(refresh_token, str):
            self._fail_operation(organization_id, profile_id, operation)
            raise PermissionError("Credential refresh token is invalid")

        account_mismatch = False
        missing_refresh = False
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            profile = connection.execute(
                "SELECT account_id,refresh FROM credential_profiles "
                "WHERE organization_id=? AND profile_id=? AND operation=?",
                (organization_id, profile_id, operation),
            ).fetchone()
            if profile is None or not operation:
                raise RuntimeError("Stale credential operation")
            if profile["account_id"] is not None and profile["account_id"] != account_id:
                connection.execute(
                    "UPDATE credential_profiles SET state='account_mismatch',operation='' "
                    "WHERE organization_id=? AND profile_id=? AND operation=?",
                    (organization_id, profile_id, operation),
                )
                account_mismatch = True
            else:
                saved_refresh = refresh_token or profile["refresh"]
                if not saved_refresh:
                    connection.execute(
                        "UPDATE credential_profiles SET state='account_mismatch',operation='' "
                        "WHERE organization_id=? AND profile_id=? AND operation=?",
                        (organization_id, profile_id, operation),
                    )
                    missing_refresh = True
                else:
                    connection.execute(
                        "UPDATE credential_profiles SET access=?,refresh=?,expires=?,account_id=?,"
                        "residency=?,generation=generation+1,state='ready',operation='' "
                        "WHERE organization_id=? AND profile_id=? AND operation=?",
                        (
                            access,
                            saved_refresh,
                            time.time() + expires_in,
                            account_id,
                            residency,
                            organization_id,
                            profile_id,
                            operation,
                        ),
                    )
        if account_mismatch:
            raise PermissionError("Credential account does not match the profile")
        if missing_refresh:
            raise PermissionError("Credential refresh token is missing")

    def profile_status(self, organization_id: str, profile_id: str) -> dict[str, object] | None:
        """Return profile metadata without exposing either OAuth token."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT organization_id,profile_id,name,account_id,expires,residency,generation,state "
                "FROM credential_profiles WHERE organization_id=? AND profile_id=?",
                (organization_id, profile_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def mark_uncertain(self, organization_id: str, profile_id: str, operation: str) -> None:
        """Fail closed after a credential operation whose external outcome is unknown."""
        with self.connect() as connection:
            connection.execute(
                "UPDATE credential_profiles SET state=?,operation='' "
                "WHERE organization_id=? AND profile_id=? AND operation=?",
                (_REFRESH_UNCERTAIN, organization_id, profile_id, operation),
            )

    def recover_interrupted(self) -> None:
        """Fail closed after this host restarts during a non-durable OAuth exchange.

        The caller owns exclusive host lifetime; initialization deliberately does not
        alter a database which may still belong to a live host process.
        """
        with self.connect() as connection:
            connection.execute(
                "UPDATE credential_profiles SET state=?,operation='' "
                "WHERE state IN ('login_pending','refreshing')",
                (_REFRESH_UNCERTAIN,),
            )

    def assigned_credential(self, key: str) -> sqlite3.Row | None:
        """Look up the one assigned profile for a host-issued agent key."""
        with self.connect() as connection:
            return connection.execute(
                "SELECT b.organization_id,b.profile_id,p.access,p.account_id,p.expires,p.residency,"
                "p.generation,p.state FROM agent_credential_bindings b "
                "JOIN credential_profiles p ON p.organization_id=b.organization_id "
                "AND p.profile_id=b.profile_id WHERE b.key_digest=?",
                (_digest(key),),
            ).fetchone()

    def profile_credential(self, organization_id: str, profile_id: str) -> sqlite3.Row | None:
        """Read a host-owned profile for a short-lived native discovery process."""
        with self.connect() as connection:
            return connection.execute(
                "SELECT organization_id,profile_id,access,account_id,expires,residency,"
                "generation,state FROM credential_profiles "
                "WHERE organization_id=? AND profile_id=?",
                (organization_id, profile_id),
            ).fetchone()

    def refresh_credential(self, organization_id: str, profile_id: str) -> sqlite3.Row | None:
        """Read private refresh state for the host service only."""
        with self.connect() as connection:
            return connection.execute(
                "SELECT access,refresh,expires,generation,state FROM credential_profiles "
                "WHERE organization_id=? AND profile_id=?",
                (organization_id, profile_id),
            ).fetchone()

    def _fail_operation(self, organization_id: str, profile_id: str, operation: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE credential_profiles SET state='account_mismatch',operation='' "
                "WHERE organization_id=? AND profile_id=? AND operation=?",
                (organization_id, profile_id, operation),
            )


class CredentialService:
    """Host-side access and serialized refresh operations for assigned agents."""

    def __init__(self, store: CredentialStore, client: httpx.AsyncClient) -> None:
        self.store = store
        self.client = client
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def access_for_agent(
        self,
        token: str,
        *,
        rejected_generation: int | None = None,
        previous_account_id: str | None = None,
    ) -> dict[str, object]:
        """Return assigned access, refreshing a rejected generation at most once.

        Native clients retain the generation they authenticated with. A late
        rejection for an older generation reuses the newer host credential.
        """
        credential = self.store.assigned_credential(token)
        if credential is None:
            raise PermissionError("Credential unavailable")
        if previous_account_id is not None and credential["account_id"] != previous_account_id:
            raise PermissionError("Credential account mismatch")
        if rejected_generation is not None and (
            type(rejected_generation) is not int
            or rejected_generation < 0
            or rejected_generation > credential["generation"]
        ):
            raise PermissionError("Credential generation mismatch")
        assignment = (credential["organization_id"], credential["profile_id"])
        needs_refresh = credential["state"] == _REFRESHING or (
            credential["state"] == _READY
            and (
                credential["expires"] <= time.time() + 30
                or rejected_generation == credential["generation"]
            )
        )
        if needs_refresh:
            await self.refresh_profile(*assignment, rejected_generation=rejected_generation)
            credential = self.store.assigned_credential(token)
        elif credential["state"] != _READY:
            raise PermissionError("Credential unavailable")
        if (
            credential is None
            or credential["state"] != _READY
            or credential["expires"] <= time.time()
            or (credential["organization_id"], credential["profile_id"]) != assignment
            or (previous_account_id is not None and credential["account_id"] != previous_account_id)
        ):
            raise PermissionError("Credential unavailable")
        return {
            "profile_id": credential["profile_id"],
            "access": credential["access"],
            "account_id": credential["account_id"],
            "expires": credential["expires"],
            "residency": credential["residency"],
            "generation": credential["generation"],
        }

    async def access_for_profile(
        self,
        organization_id: str,
        profile_id: str,
        *,
        rejected_generation: int | None = None,
        previous_account_id: str | None = None,
    ) -> dict[str, object]:
        """Broker one profile to an isolated host-owned App Server process."""
        credential = self.store.profile_credential(organization_id, profile_id)
        if credential is None:
            raise PermissionError("Credential unavailable")
        if previous_account_id is not None and credential["account_id"] != previous_account_id:
            raise PermissionError("Credential account mismatch")
        if rejected_generation is not None and (
            type(rejected_generation) is not int
            or rejected_generation < 0
            or rejected_generation > credential["generation"]
        ):
            raise PermissionError("Credential generation mismatch")
        needs_refresh = credential["state"] == _REFRESHING or (
            credential["state"] == _READY
            and (
                credential["expires"] <= time.time() + 30
                or rejected_generation == credential["generation"]
            )
        )
        if needs_refresh:
            await self.refresh_profile(
                organization_id, profile_id, rejected_generation=rejected_generation
            )
            credential = self.store.profile_credential(organization_id, profile_id)
        elif credential["state"] != _READY:
            raise PermissionError("Credential unavailable")
        if (
            credential is None
            or credential["state"] != _READY
            or credential["expires"] <= time.time()
            or credential["organization_id"] != organization_id
            or credential["profile_id"] != profile_id
            or (previous_account_id is not None and credential["account_id"] != previous_account_id)
        ):
            raise PermissionError("Credential unavailable")
        return {
            "profile_id": credential["profile_id"],
            "access": credential["access"],
            "account_id": credential["account_id"],
            "expires": credential["expires"],
            "residency": credential["residency"],
            "generation": credential["generation"],
        }

    async def refresh_profile(
        self,
        organization_id: str,
        profile_id: str,
        *,
        rejected_generation: int | None = None,
    ) -> None:
        """Refresh expiry or a rejected generation; retain uncertain rotation outcomes."""
        lock = self._locks.setdefault((organization_id, profile_id), asyncio.Lock())
        async with lock:
            credential = self.store.refresh_credential(organization_id, profile_id)
            if credential is None or credential["state"] != _READY:
                raise PermissionError("Credential unavailable")
            if (
                credential["expires"] > time.time() + 30
                and credential["generation"] != rejected_generation
            ):
                return
            operation = self.store.acquire_operation(
                organization_id,
                profile_id,
                [_READY],
                _REFRESHING,
                expected_generation=credential["generation"],
            )
            if operation is None:
                current = self.store.refresh_credential(organization_id, profile_id)
                if (
                    current is not None
                    and current["state"] == _READY
                    and current["expires"] > time.time() + 30
                ):
                    return
                raise PermissionError("Credential unavailable")
            try:
                refresh_token = credential["refresh"]
                if not refresh_token:
                    raise ValueError("Credential refresh token is missing")
                response = await self.client.post(
                    f"{OPENAI_AUTH_ISSUER}/oauth/token",
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "client_id": OPENAI_CODEX_CLIENT_ID,
                    },
                )
                response.raise_for_status()
                tokens = response.json()
                if not isinstance(tokens, Mapping):
                    raise TypeError("OAuth token response is invalid")
                self.store.save_tokens(organization_id, profile_id, tokens, operation)
            except asyncio.CancelledError:
                self.store.mark_uncertain(organization_id, profile_id, operation)
                raise
            except (httpx.HTTPError, PermissionError, RuntimeError, TypeError, ValueError):
                self.store.mark_uncertain(organization_id, profile_id, operation)
                raise PermissionError("Credential refresh needs reconciliation") from None


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _prepare_private_database_file(path: Path) -> None:
    """Create host credential state atomically or reject public existing state."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    except FileExistsError:
        if not path.parent.is_dir():
            raise ValueError("Credential state path must be a directory") from None
        raise
    if path.parent.stat().st_mode & 0o077:
        raise ValueError("Existing credential state directory must be private")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if not path.is_file() or path.is_symlink():
            raise ValueError("Credential database path must be a regular file") from None
        if path.stat().st_mode & 0o077:
            raise ValueError("Existing credential database must be private")
    else:
        os.close(descriptor)


def _token_metadata(tokens: Mapping[str, object]) -> tuple[str, str, str | None, float]:
    access = tokens.get("access_token")
    if not isinstance(access, str) or not access:
        raise ValueError("OAuth access token is missing")
    claims = [_jwt_claims(access)]
    identity = tokens.get("id_token")
    if identity is not None:
        if not isinstance(identity, str):
            raise ValueError("OAuth identity token is invalid")
        claims.append(_jwt_claims(identity))
    accounts = {_account_id(claim) for claim in claims}
    accounts.discard(None)
    if len(accounts) != 1:
        raise ValueError("OAuth account claim is missing or inconsistent")
    expires_in = tokens.get("expires_in")
    if not isinstance(expires_in, (int, float)) or isinstance(expires_in, bool):
        raise TypeError("OAuth expiration is invalid")
    if expires_in <= 0 or not math.isfinite(expires_in):
        raise ValueError("OAuth expiration is invalid")
    account_id = accounts.pop()
    assert account_id is not None
    return access, account_id, _residency(claims[0]), float(expires_in)


def _jwt_claims(token: str) -> Mapping[str, Any]:
    try:
        _, payload, _ = token.split(".")
        decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        claims = json.loads(decoded)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("OAuth claims are invalid") from error
    if not isinstance(claims, dict):
        raise TypeError("OAuth claims are invalid")
    return claims


def _account_id(claims: Mapping[str, Any]) -> str | None:
    nested = claims.get("https://api.openai.com/auth")
    nested_claims = nested if isinstance(nested, Mapping) else {}
    for value in (
        claims.get("chatgpt_account_id"),
        nested_claims.get("chatgpt_account_id"),
    ):
        if isinstance(value, str) and value:
            return value
    organizations = claims.get("organizations")
    if isinstance(organizations, list) and organizations:
        organization = organizations[0]
        if isinstance(organization, Mapping):
            identifier = organization.get("id")
            if isinstance(identifier, str) and identifier:
                return identifier
    return None


def _residency(claims: Mapping[str, Any]) -> str | None:
    nested = claims.get("https://api.openai.com/auth")
    nested_claims = nested if isinstance(nested, Mapping) else {}
    value = nested_claims.get("chatgpt_compute_residency", claims.get("chatgpt_compute_residency"))
    return value if isinstance(value, str) and value else None
