"""SQLite ownership for Fesnyng service identity and schema state."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from os import O_CREAT, O_EXCL, O_WRONLY, close
from os import open as open_file
from pathlib import Path
from uuid import UUID, uuid4

from fesnyng_backend.settings import ServiceName, ServiceSettings

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class DurableServiceIdentity:
    service: ServiceName
    instance_id: UUID
    schema_version: int


def initialize_service_state(settings: ServiceSettings) -> DurableServiceIdentity:
    """Create or validate the service-owned SQLite schema and identity."""

    _prepare_private_state_directory(settings.state_directory)
    _prepare_private_database_file(settings.database_path)

    connection = sqlite3.connect(settings.database_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_metadata (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                service TEXT NOT NULL,
                schema_version INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS service_identity (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                instance_id TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO schema_metadata (singleton, service, schema_version)
            VALUES (1, ?, ?)
            """,
            (settings.service, SCHEMA_VERSION),
        )
        owner, schema_version = connection.execute(
            "SELECT service, schema_version FROM schema_metadata WHERE singleton = 1"
        ).fetchone()
        if owner != settings.service:
            raise ValueError(f"SQLite state is owned by {owner}, not {settings.service}.")
        if schema_version != SCHEMA_VERSION:
            raise RuntimeError(
                f"Unsupported schema version {schema_version}; expected {SCHEMA_VERSION}."
            )

        row = connection.execute(
            "SELECT instance_id FROM service_identity WHERE singleton = 1"
        ).fetchone()
        if row is None:
            instance_id = settings.requested_instance_id or uuid4()
            connection.execute(
                "INSERT INTO service_identity (singleton, instance_id) VALUES (1, ?)",
                (str(instance_id),),
            )
        else:
            instance_id = UUID(row[0])
            if (
                settings.requested_instance_id is not None
                and settings.requested_instance_id != instance_id
            ):
                raise ValueError("Configured instance ID does not match durable service identity.")
        connection.commit()
    finally:
        connection.close()

    return DurableServiceIdentity(
        service=settings.service,
        instance_id=instance_id,
        schema_version=schema_version,
    )


def _prepare_private_state_directory(path: Path) -> None:
    try:
        path.mkdir(parents=True, mode=0o700)
    except FileExistsError:
        if not path.is_dir():
            raise ValueError("State path must be a directory.") from None
        if path.stat().st_mode & 0o077:
            raise ValueError("Existing state directory must be private.")


def _prepare_private_database_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = open_file(path, O_WRONLY | O_CREAT | O_EXCL, 0o600)
    except FileExistsError:
        if not path.is_file():
            raise ValueError("SQLite database path must be a file.") from None
        if path.stat().st_mode & 0o077:
            raise ValueError("Existing SQLite database must be private.")
    else:
        close(descriptor)
