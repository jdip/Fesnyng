"""Durable, explicit agent memory owned by an agent host."""

import sqlite3
from typing import Any

from pydantic import Field

from fesnyng_backend.agent_models import Contract, Slug
from fesnyng_backend.host_models import Actor
from fesnyng_backend.host_store import HostStore


class MemoryWrite(Contract):
    key: Slug
    content: str = Field(max_length=200_000)
    expected_revision: int = Field(ge=0)


class HostMemoryWrite(MemoryWrite):
    author: Actor


class MemoryStore:
    def __init__(self, host_store: HostStore):
        self.host_store = host_store

    def initialize(self) -> None:
        with self.host_store.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS host_memory (
                    organization_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    content TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK(revision > 0),
                    author_kind TEXT NOT NULL,
                    author_id TEXT NOT NULL,
                    author_name TEXT NOT NULL,
                    author_session_id TEXT,
                    updated_at INTEGER NOT NULL DEFAULT (unixepoch()),
                    PRIMARY KEY(organization_id, agent_id, key),
                    FOREIGN KEY(organization_id, agent_id)
                        REFERENCES host_agents(organization_id, agent_id)
                );
            """)

    def list(self, organization_id: str, agent_id: str) -> list[dict[str, Any]]:
        self.host_store.agent(organization_id, agent_id)
        with self.host_store.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM host_memory WHERE organization_id=? AND agent_id=? ORDER BY key",
                (organization_id, agent_id),
            ).fetchall()
        return [_memory(row) for row in rows]

    def get(self, organization_id: str, agent_id: str, key: str) -> dict[str, Any] | None:
        self.host_store.agent(organization_id, agent_id)
        write = MemoryWrite(key=key, content="", expected_revision=0)
        with self.host_store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM host_memory WHERE organization_id=? AND agent_id=? AND key=?",
                (organization_id, agent_id, write.key),
            ).fetchone()
        return _memory(row) if row else None

    def put(
        self,
        organization_id: str,
        agent_id: str,
        key: str,
        content: str,
        expected_revision: int,
        author: Actor,
    ) -> dict[str, Any]:
        self.host_store.agent(organization_id, agent_id)
        write = MemoryWrite(key=key, content=content, expected_revision=expected_revision)
        with self.host_store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if write.expected_revision == 0:
                try:
                    connection.execute(
                        """INSERT INTO host_memory(
                            organization_id, agent_id, key, content, revision,
                            author_kind, author_id, author_name, author_session_id
                        ) VALUES(?,?,?,?,1,?,?,?,?)""",
                        (
                            organization_id,
                            agent_id,
                            write.key,
                            write.content,
                            author.kind,
                            str(author.id),
                            author.name,
                            author.session_id,
                        ),
                    )
                except sqlite3.IntegrityError:
                    raise ValueError("Memory revision conflict") from None
            else:
                changed = connection.execute(
                    """UPDATE host_memory
                    SET content=?, revision=revision+1, author_kind=?, author_id=?, author_name=?,
                        author_session_id=?, updated_at=unixepoch()
                    WHERE organization_id=? AND agent_id=? AND key=? AND revision=?""",
                    (
                        write.content,
                        author.kind,
                        str(author.id),
                        author.name,
                        author.session_id,
                        organization_id,
                        agent_id,
                        write.key,
                        write.expected_revision,
                    ),
                ).rowcount
                if changed != 1:
                    raise ValueError("Memory revision conflict")
            row = connection.execute(
                "SELECT * FROM host_memory WHERE organization_id=? AND agent_id=? AND key=?",
                (organization_id, agent_id, write.key),
            ).fetchone()
        if row is None:
            raise RuntimeError("Memory write did not persist")
        return _memory(row)


def _memory(row: Any) -> dict[str, Any]:
    return {
        "key": row["key"],
        "content": row["content"],
        "revision": row["revision"],
        "author": {
            "kind": row["author_kind"],
            "id": row["author_id"],
            "name": row["author_name"],
            "session_id": row["author_session_id"],
        },
        "updated_at": row["updated_at"],
    }
