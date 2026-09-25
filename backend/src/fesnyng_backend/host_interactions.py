"""Durable host-owned native interactions and per-thread permission policy."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable, Mapping
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import TypeAdapter, ValidationError

from fesnyng_backend.agent_models import PermissionRule
from fesnyng_backend.host_models import (
    Actor,
    HostAgentConfiguration,
    NativeID,
    permission_rules,
)
from fesnyng_backend.host_native_sessions import native_children, visit_native_child
from fesnyng_backend.host_runtime import RuntimeRouter, RuntimeUnavailable
from fesnyng_backend.host_store import HostStore

InteractionKind = Literal["question", "permission"]
_native_id = TypeAdapter(NativeID)


class NativeRuntime(Protocol):
    def lock(self, agent_id: str) -> asyncio.Lock: ...

    async def request(
        self,
        organization_id: str,
        agent_id: str,
        path: str,
        *,
        method: str = "GET",
        body: Any = None,
        directory: str | None = None,
    ) -> Any: ...


class Interactions:
    def __init__(self, host: HostStore, runtime: NativeRuntime):
        self.host = host
        self.runtime = runtime
        # The Dispatcher owns in-flight prompt admission.  Agent-host wiring may
        # supply this without making Interactions depend on Dispatcher directly.
        self.native_admissions_settled: Callable[[str, str, str], bool] | None = None

    def initialize(self) -> None:
        with self.host.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS host_interaction_operations (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    session_id TEXT NOT NULL REFERENCES host_sessions(session_id),
                    request_id TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('question', 'permission')),
                    answer TEXT NOT NULL,
                    author TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('submitting', 'completed', 'uncertain')),
                    error TEXT,
                    created_at INTEGER NOT NULL DEFAULT (unixepoch()),
                    updated_at INTEGER NOT NULL DEFAULT (unixepoch()),
                    FOREIGN KEY(organization_id, agent_id)
                        REFERENCES host_agents(organization_id, agent_id)
                );
                CREATE TABLE IF NOT EXISTS host_thread_policy (
                    organization_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    session_id TEXT NOT NULL REFERENCES host_sessions(session_id),
                    desired_revision INTEGER NOT NULL DEFAULT 0,
                    applied_revision INTEGER NOT NULL DEFAULT 0,
                    applied_policy_version INTEGER NOT NULL DEFAULT 0,
                    overrides TEXT NOT NULL DEFAULT '[]',
                    author TEXT,
                    updated_at INTEGER NOT NULL DEFAULT (unixepoch()),
                    PRIMARY KEY(organization_id, agent_id, session_id),
                    FOREIGN KEY(organization_id, agent_id)
                        REFERENCES host_agents(organization_id, agent_id)
                );
            """)

    def recover_interrupted(self) -> None:
        """Mark in-flight replies uncertain after exclusive host startup recovery."""
        with self.host.connect() as connection:
            connection.execute(
                """UPDATE host_interaction_operations
                SET state='uncertain',
                    error='Host restarted during native interaction reply; inspect before retrying',
                    updated_at=unixepoch()
                WHERE state='submitting'"""
            )

    async def pending(
        self, organization_id: str, agent_id: str, session_id: str, kind: InteractionKind
    ) -> list[dict[str, Any]]:
        mapped = self.host.session(organization_id, agent_id, session_id)
        if mapped["frozen_at"] is not None:
            return []
        session = self._opencode_session(organization_id, agent_id, session_id)
        result = await self.runtime.request(
            organization_id, agent_id, f"/{kind}", directory=session["directory"]
        )
        if not isinstance(result, list) or not all(isinstance(entry, dict) for entry in result):
            raise RuntimeUnavailable("Native pending interactions response is invalid")
        return [entry for entry in result if entry.get("sessionID") == session_id]

    async def maintenance_pending(self, organization_id: str, agent_id: str) -> bool:
        """Check both native harnesses for a prompt requiring a human response."""
        for session in self.host.sessions(organization_id, agent_id):
            if session["frozen_at"] is not None:
                continue
            if session["runtime_type"] == "codex":
                router = getattr(self.runtime, "runtime_router", None)
                adapter = (
                    router.for_session(session)
                    if router is not None
                    else getattr(self.runtime, "codex", None)
                )
                if adapter is None or not hasattr(adapter, "pending"):
                    raise RuntimeUnavailable("Codex pending interaction status is unavailable")
                pending = await adapter.pending(organization_id, agent_id, session["session_id"])
                if not isinstance(pending, list):
                    raise RuntimeUnavailable("Codex pending interaction status is invalid")
                if pending:
                    return True
            else:
                for kind in ("question", "permission"):
                    if await self.pending(organization_id, agent_id, session["session_id"], kind):
                        return True
        return False

    async def reply(
        self,
        organization_id: str,
        agent_id: str,
        session_id: str,
        operation_id: UUID,
        request_id: str,
        kind: InteractionKind,
        answer: object,
        author: Actor,
    ) -> dict[str, Any]:
        if kind not in {"question", "permission"}:
            raise ValueError("Unknown native interaction kind")
        self.host.require_writable(organization_id, agent_id, session_id)
        payload, path = _reply_request(kind, request_id, answer)
        session = self._opencode_session(organization_id, agent_id, session_id)
        async with self.runtime.lock(agent_id):
            self.host.require_writable(organization_id, agent_id, session_id)
            if self._has_operation(str(operation_id)):
                return self._start_reply(
                    organization_id,
                    agent_id,
                    session_id,
                    operation_id,
                    request_id,
                    kind,
                    answer,
                    author,
                )
            self._require_reply_admission(organization_id, agent_id, session_id)
            pending = await self.pending(organization_id, agent_id, session_id, kind)
            if not any(
                item.get("id") == request_id and item.get("sessionID") == session_id
                for item in pending
            ):
                raise ValueError("Native interaction is not pending for this thread")
            self._require_reply_admission(organization_id, agent_id, session_id)
            receipt = self._start_reply(
                organization_id,
                agent_id,
                session_id,
                operation_id,
                request_id,
                kind,
                answer,
                author,
            )
            if receipt["state"] != "submitting":
                return receipt
            try:
                await self.runtime.request(
                    organization_id,
                    agent_id,
                    path,
                    method="POST",
                    body=payload,
                    directory=session["directory"],
                )
            except RuntimeUnavailable as error:
                self._set_operation_state(str(operation_id), "uncertain", str(error))
                raise
            except asyncio.CancelledError:
                self._set_operation_state(
                    str(operation_id),
                    "uncertain",
                    "Host stopped during native interaction reply; inspect before retrying",
                )
                raise
        self._set_operation_state(str(operation_id), "completed")
        return self._operation(organization_id, agent_id, str(operation_id))

    async def reject_question(
        self,
        organization_id: str,
        agent_id: str,
        session_id: str,
        operation_id: UUID,
        request_id: str,
        author: Actor,
    ) -> dict[str, Any]:
        """Persist a native question rejection before asking OpenCode to reject it."""
        return await self.reply(
            organization_id,
            agent_id,
            session_id,
            operation_id,
            request_id,
            "question",
            {"reject": True},
            author,
        )

    async def reply_codex(
        self,
        organization_id: str,
        agent_id: str,
        session_id: str,
        operation_id: UUID,
        request_id: str,
        response: dict[str, Any],
        author: Actor,
    ) -> dict[str, Any]:
        """Durably acknowledge one App Server request without replaying it.

        The existing interaction receipt store remains the recovery authority.
        ``permission`` is its generic one-shot native-response category; the
        opaque response remains in ``answer`` so App Server request methods do
        not acquire a lossy local taxonomy.
        """
        session = self.host.session(organization_id, agent_id, session_id)
        self.host.require_writable(organization_id, agent_id, session_id)
        if session["runtime_type"] != "codex":
            raise RuntimeUnavailable("Thread is not bound to the Codex harness")
        router = getattr(self.runtime, "runtime_router", None)
        adapter = (
            router.for_session(session)
            if router is not None
            else getattr(self.runtime, "codex", None)
        )
        if adapter is None or not all(hasattr(adapter, name) for name in ("pending", "respond")):
            raise RuntimeUnavailable("Codex harness is not available on this host")
        async with self.runtime.lock(agent_id):
            self.host.require_writable(organization_id, agent_id, session_id)
            if self._has_operation(str(operation_id)):
                return self._start_reply(
                    organization_id,
                    agent_id,
                    session_id,
                    operation_id,
                    request_id,
                    "permission",
                    response,
                    author,
                )
            envelope = self._applied_envelope(organization_id, agent_id)
            if envelope.configuration.runtime_type != "codex":
                raise RuntimeUnavailable("Codex configuration is not applied")
            self._require_reply_admission(organization_id, agent_id, session_id)
            pending = await adapter.pending(organization_id, agent_id, session_id)
            if not isinstance(pending, list) or not any(
                isinstance(item, dict) and item.get("id") == request_id for item in pending
            ):
                raise ValueError("Codex request is not pending for this thread")
            receipt = self._start_reply(
                organization_id,
                agent_id,
                session_id,
                operation_id,
                request_id,
                "permission",
                response,
                author,
            )
            if receipt["state"] != "submitting":
                return receipt
            try:
                await adapter.respond(organization_id, agent_id, session_id, request_id, response)
            except RuntimeUnavailable as error:
                self._set_operation_state(str(operation_id), "uncertain", str(error))
                raise
            except asyncio.CancelledError:
                self._set_operation_state(
                    str(operation_id),
                    "uncertain",
                    "Host stopped during Codex interaction reply; inspect before retrying",
                )
                raise
        self._set_operation_state(str(operation_id), "completed")
        return self._operation(organization_id, agent_id, str(operation_id))

    async def reply_scoped(
        self,
        organization_id: str,
        agent_id: str,
        owner_session_id: str,
        native_session_id: str,
        native_directory: str,
        operation_id: UUID,
        request_id: str,
        kind: InteractionKind,
        answer: object,
        author: Actor,
    ) -> dict[str, Any]:
        """Reply to a proven native child while retaining its mapped root as owner.

        Child sessions are native execution descendants, not independently
        configured Fesnyng threads.  The durable receipt and policy therefore
        remain on the mapped root, while native pending/reply calls use the
        verified child's session and directory.
        """
        if kind not in {"question", "permission"}:
            raise ValueError("Unknown native interaction kind")
        payload, path = _reply_request(kind, request_id, answer)
        self.host.require_writable(organization_id, agent_id, owner_session_id)
        self._opencode_session(organization_id, agent_id, owner_session_id)
        async with self.runtime.lock(agent_id):
            self.host.require_writable(organization_id, agent_id, owner_session_id)
            if self._has_operation(str(operation_id)):
                return self._start_reply(
                    organization_id,
                    agent_id,
                    owner_session_id,
                    operation_id,
                    request_id,
                    kind,
                    answer,
                    author,
                )
            self._require_reply_admission(organization_id, agent_id, owner_session_id)
            pending = await self._pending_native(
                organization_id, agent_id, native_session_id, native_directory, kind
            )
            if not any(item.get("id") == request_id for item in pending):
                raise ValueError("Native interaction is not pending for this thread")
            self._require_reply_admission(organization_id, agent_id, owner_session_id)
            receipt = self._start_reply(
                organization_id,
                agent_id,
                owner_session_id,
                operation_id,
                request_id,
                kind,
                answer,
                author,
            )
            if receipt["state"] != "submitting":
                return receipt
            try:
                await self.runtime.request(
                    organization_id,
                    agent_id,
                    path,
                    method="POST",
                    body=payload,
                    directory=native_directory,
                )
            except RuntimeUnavailable as error:
                self._set_operation_state(str(operation_id), "uncertain", str(error))
                raise
            except asyncio.CancelledError:
                self._set_operation_state(
                    str(operation_id),
                    "uncertain",
                    "Host stopped during native interaction reply; inspect before retrying",
                )
                raise
        self._set_operation_state(str(operation_id), "completed")
        return self._operation(organization_id, agent_id, str(operation_id))

    def get_policy(self, organization_id: str, agent_id: str, session_id: str) -> dict[str, Any]:
        envelope = self._applied_envelope(organization_id, agent_id)
        record = self._policy_record(organization_id, agent_id, session_id)
        return _policy_receipt(record, envelope)

    def get_reply(
        self,
        organization_id: str,
        agent_id: str,
        session_id: str,
        operation_id: UUID,
        request_id: str,
        kind: InteractionKind,
    ) -> dict[str, Any]:
        self.host.session(organization_id, agent_id, session_id)
        receipt = self._operation(organization_id, agent_id, str(operation_id))
        if (
            receipt["session_id"] != session_id
            or receipt["request_id"] != request_id
            or receipt["kind"] != kind
        ):
            raise LookupError("Interaction operation does not belong to this native request")
        return receipt

    def existing_reply(
        self,
        organization_id: str,
        agent_id: str,
        operation_id: UUID,
        request_id: str,
        kind: InteractionKind,
        answer: object,
        author: Actor,
    ) -> dict[str, Any] | None:
        """Return a matching durable receipt before a retry rechecks native pending state."""
        try:
            receipt = self._operation(organization_id, agent_id, str(operation_id))
        except LookupError:
            return None
        if (
            receipt["request_id"] != request_id
            or receipt["kind"] != kind
            or receipt["answer"] != answer
            or receipt["author"] != author.model_dump(mode="json")
        ):
            raise ValueError("Interaction operation identity conflict")
        return receipt

    def put_policy(
        self,
        organization_id: str,
        agent_id: str,
        session_id: str,
        expected_revision: int,
        rules: list[PermissionRule],
        author: Actor,
    ) -> dict[str, Any]:
        if expected_revision < 0:
            raise ValueError("Policy revision must be non-negative")
        envelope = self._applied_envelope(organization_id, agent_id)
        if not envelope.policy.allow_thread_overrides:
            raise ValueError("Thread policy overrides are disabled")
        self.host.session(organization_id, agent_id, session_id)
        validated = [PermissionRule.model_validate(rule) for rule in rules]
        overrides = json.dumps([rule.model_dump() for rule in validated], sort_keys=True)
        attribution = author.model_dump_json()
        with self.host.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.host.require_writable(organization_id, agent_id, session_id, connection=connection)
            row = connection.execute(
                "SELECT * FROM host_thread_policy WHERE organization_id=? AND agent_id=? AND session_id=?",
                (organization_id, agent_id, session_id),
            ).fetchone()
            desired = row["desired_revision"] if row else 0
            if desired != expected_revision:
                raise ValueError("Thread policy revision conflict")
            if row is None:
                connection.execute(
                    """INSERT INTO host_thread_policy(
                        organization_id, agent_id, session_id, desired_revision, overrides, author
                    ) VALUES(?,?,?,?,?,?)""",
                    (organization_id, agent_id, session_id, 1, overrides, attribution),
                )
            else:
                connection.execute(
                    """UPDATE host_thread_policy
                    SET desired_revision=?, overrides=?, author=?, updated_at=unixepoch()
                    WHERE organization_id=? AND agent_id=? AND session_id=?""",
                    (desired + 1, overrides, attribution, organization_id, agent_id, session_id),
                )
        return self.get_policy(organization_id, agent_id, session_id)

    async def apply_policy(
        self,
        organization_id: str,
        agent_id: str,
        session_id: str,
        candidate: HostAgentConfiguration | None = None,
    ) -> dict[str, Any]:
        self.host.require_writable(organization_id, agent_id, session_id)
        session = self.host.session(organization_id, agent_id, session_id)
        envelope = candidate or self._applied_envelope(organization_id, agent_id)
        if str(envelope.organization_id) != organization_id or str(envelope.agent_id) != agent_id:
            raise ValueError("Policy configuration belongs to another agent")
        if session["runtime_type"] == "codex":
            return await self._apply_codex_policy(organization_id, agent_id, session_id, envelope)
        RuntimeRouter.require_supported(session["runtime_type"])
        async with self.runtime.lock(agent_id):
            self.host.require_writable(organization_id, agent_id, session_id)
            record = self._policy_record(organization_id, agent_id, session_id)
            if (
                record["desired_revision"] == record["applied_revision"]
                and record["applied_policy_version"] == envelope.policy_version
            ):
                return _policy_receipt(record, envelope)
            overrides = [] if not envelope.policy.allow_thread_overrides else record["overrides"]
            suffix = permission_rules(envelope, overrides)
            sessions = await self._session_family(
                organization_id, agent_id, session_id, session["directory"]
            )
            await self._quiesce_sessions(organization_id, agent_id, sessions)
            if self.native_admissions_settled is not None and not self.native_admissions_settled(
                organization_id, agent_id, session_id
            ):
                raise RuntimeUnavailable("Thread policy change is waiting for native admissions")
            await self._apply_suffix(organization_id, agent_id, sessions, suffix)
            applied = self._mark_policy_applied(
                organization_id,
                agent_id,
                session_id,
                record["desired_revision"],
                envelope.policy_version,
            )
        return _policy_receipt(applied, envelope)

    async def _apply_codex_policy(
        self,
        organization_id: str,
        agent_id: str,
        session_id: str,
        envelope: HostAgentConfiguration,
    ) -> dict[str, Any]:
        """Apply only the policy App Server can represent, then record that receipt."""
        router = getattr(self.runtime, "runtime_router", None)
        session = self.host.session(organization_id, agent_id, session_id)
        self.host.require_writable(organization_id, agent_id, session_id)
        adapter = (
            router.for_session(session)
            if router is not None
            else getattr(self.runtime, "codex", None)
        )
        if adapter is None or not hasattr(adapter, "apply_policy"):
            raise RuntimeUnavailable("Codex harness is not available on this host")
        async with self.runtime.lock(agent_id):
            return await self.apply_policy_locked(organization_id, agent_id, session_id, envelope)

    async def apply_policy_locked(
        self,
        organization_id: str,
        agent_id: str,
        session_id: str,
        candidate: HostAgentConfiguration | None = None,
    ) -> dict[str, Any]:
        """Apply a Codex policy while the caller already holds the agent runtime lock."""
        self.host.require_writable(organization_id, agent_id, session_id)
        session = self.host.session(organization_id, agent_id, session_id)
        envelope = candidate or self._applied_envelope(organization_id, agent_id)
        if session["runtime_type"] != "codex":
            raise RuntimeUnavailable("Thread is not bound to the Codex harness")
        router = getattr(self.runtime, "runtime_router", None)
        adapter = (
            router.for_session(session)
            if router is not None
            else getattr(self.runtime, "codex", None)
        )
        if adapter is None or not hasattr(adapter, "apply_policy"):
            raise RuntimeUnavailable("Codex harness is not available on this host")
        record = self._policy_record(organization_id, agent_id, session_id)
        if (
            record["desired_revision"] == record["applied_revision"]
            and record["applied_policy_version"] == envelope.policy_version
        ):
            return _policy_receipt(record, envelope)
        if self.native_admissions_settled is not None and not self.native_admissions_settled(
            organization_id, agent_id, session_id
        ):
            raise RuntimeUnavailable("Thread policy change is waiting for native admissions")
        await adapter.apply_policy(
            organization_id, agent_id, session_id, envelope, record["overrides"]
        )
        applied = self._mark_policy_applied(
            organization_id,
            agent_id,
            session_id,
            record["desired_revision"],
            envelope.policy_version,
        )
        return _policy_receipt(applied, envelope)

    async def _session_family(
        self, organization_id: str, agent_id: str, root_id: str, root_directory: str
    ) -> list[tuple[str, str]]:
        """Discover only descendants reported by this agent's native session tree."""
        root = _validated_native_id(root_id)
        sessions = [(root, root_directory)]
        seen = {root}
        pending = [(root, root_directory)]
        while pending:
            parent_id, directory = pending.pop()
            children = await self.runtime.request(
                organization_id,
                agent_id,
                f"/session/{parent_id}/children",
                directory=directory,
            )
            for child_id, child_directory in native_children(
                children, parent_id, require_nonempty_directory=True
            ):
                visit_native_child(child_id, seen)
                sessions.append((child_id, child_directory))
                pending.append((child_id, child_directory))
        return sessions

    async def _quiesce_sessions(
        self, organization_id: str, agent_id: str, sessions: list[tuple[str, str]]
    ) -> None:
        active = [
            session
            for session in sessions
            if not await self._idle(organization_id, agent_id, session[0], session[1])
        ]
        for native_id, directory in active:
            acknowledged = await self.runtime.request(
                organization_id,
                agent_id,
                f"/session/{native_id}/abort",
                method="POST",
                body={},
                directory=directory,
            )
            if acknowledged is not True:
                raise RuntimeUnavailable("Native abort has no verified receipt")
        for _ in range(30):
            if all(
                [
                    await self._idle(organization_id, agent_id, native_id, directory)
                    for native_id, directory in sessions
                ]
            ):
                return
            await asyncio.sleep(0.1)
        raise RuntimeUnavailable("Thread policy change is waiting for native sessions to stop")

    async def _apply_suffix(
        self,
        organization_id: str,
        agent_id: str,
        sessions: list[tuple[str, str]],
        suffix: list[dict[str, str]],
    ) -> None:
        for native_id, directory in sessions:
            await self.runtime.request(
                organization_id,
                agent_id,
                "/instance/dispose",
                method="POST",
                body={},
                directory=directory,
            )
            await self.runtime.request(
                organization_id,
                agent_id,
                f"/session/{native_id}",
                method="PATCH",
                body={"permission": suffix},
                directory=directory,
            )
            effective = await self.runtime.request(
                organization_id,
                agent_id,
                f"/session/{native_id}",
                directory=directory,
            )
            if (
                not isinstance(effective, Mapping)
                or not isinstance(effective.get("permission"), list)
                or effective["permission"][-len(suffix) :] != suffix
            ):
                raise RuntimeUnavailable("Native thread policy was not applied")

    async def reconcile_once(self) -> dict[str, str]:
        """Retry settled thread-policy changes without replaying a global configuration change."""
        if self.host.maintenance_status()["state"] == "closed":
            return {}
        with self.host.connect() as connection:
            rows = connection.execute(
                """SELECT policy.organization_id, policy.agent_id, policy.session_id
                FROM host_thread_policy AS policy
                JOIN host_agents AS agent
                  ON agent.organization_id=policy.organization_id
                 AND agent.agent_id=policy.agent_id
                JOIN host_sessions AS session
                  ON session.organization_id=policy.organization_id
                 AND session.agent_id=policy.agent_id
                 AND session.session_id=policy.session_id
                 AND session.deleted_at IS NULL
                 AND session.frozen_at IS NULL
                WHERE policy.desired_revision != policy.applied_revision
                  AND agent.applied_envelope IS NOT NULL
                 AND agent.desired_envelope=agent.applied_envelope
                ORDER BY policy.organization_id, policy.agent_id, policy.session_id"""
            ).fetchall()
        result: dict[str, str] = {}
        for row in rows:
            organization_id, agent_id, session_id = (
                row["organization_id"],
                row["agent_id"],
                row["session_id"],
            )
            key = f"{organization_id}/{agent_id}/{session_id}"
            try:
                await self.apply_policy(organization_id, agent_id, session_id)
            except (RuntimeUnavailable, ValueError):
                result[key] = "pending"
            else:
                result[key] = "applied"
        return result

    def _start_reply(
        self,
        organization_id: str,
        agent_id: str,
        session_id: str,
        operation_id: UUID,
        request_id: str,
        kind: InteractionKind,
        answer: object,
        author: Actor,
    ) -> dict[str, Any]:
        encoded_answer = json.dumps(answer, sort_keys=True, separators=(",", ":"))
        encoded_author = author.model_dump_json()
        with self.host.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT * FROM host_interaction_operations WHERE id=?", (str(operation_id),)
            ).fetchone()
            if previous:
                expected = (organization_id, agent_id, session_id, request_id, kind, encoded_answer)
                actual = tuple(
                    previous[field]
                    for field in (
                        "organization_id",
                        "agent_id",
                        "session_id",
                        "request_id",
                        "kind",
                        "answer",
                    )
                )
                if actual != expected or previous["author"] != encoded_author:
                    raise ValueError("Interaction operation identity conflict")
                return _operation(previous)
            connection.execute(
                """INSERT INTO host_interaction_operations(
                    id, organization_id, agent_id, session_id, request_id, kind, answer, author, state
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    str(operation_id),
                    organization_id,
                    agent_id,
                    session_id,
                    request_id,
                    kind,
                    encoded_answer,
                    encoded_author,
                    "submitting",
                ),
            )
        return self._operation(organization_id, agent_id, str(operation_id))

    def _operation(self, organization_id: str, agent_id: str, operation_id: str) -> dict[str, Any]:
        with self.host.connect() as connection:
            row = connection.execute(
                """SELECT * FROM host_interaction_operations
                WHERE id=? AND organization_id=? AND agent_id=?""",
                (operation_id, organization_id, agent_id),
            ).fetchone()
        if row is None:
            raise LookupError("Interaction operation not found")
        return _operation(row)

    def _has_operation(self, operation_id: str) -> bool:
        with self.host.connect() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM host_interaction_operations WHERE id=?", (operation_id,)
                ).fetchone()
                is not None
            )

    def _set_operation_state(self, operation_id: str, state: str, error: str | None = None) -> None:
        with self.host.connect() as connection:
            connection.execute(
                """UPDATE host_interaction_operations
                SET state=?, error=?, updated_at=unixepoch() WHERE id=? AND state='submitting'""",
                (state, error, operation_id),
            )

    def _policy_record(
        self, organization_id: str, agent_id: str, session_id: str
    ) -> dict[str, Any]:
        self.host.session(organization_id, agent_id, session_id)
        with self.host.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM host_thread_policy WHERE organization_id=? AND agent_id=? AND session_id=?",
                (organization_id, agent_id, session_id),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO host_thread_policy(organization_id,agent_id,session_id) VALUES(?,?,?)",
                    (organization_id, agent_id, session_id),
                )
                row = connection.execute(
                    "SELECT * FROM host_thread_policy WHERE organization_id=? AND agent_id=? AND session_id=?",
                    (organization_id, agent_id, session_id),
                ).fetchone()
        if row is None:
            raise RuntimeError("Thread policy did not persist")
        return _policy(row)

    def _mark_policy_applied(
        self,
        organization_id: str,
        agent_id: str,
        session_id: str,
        expected_desired: int,
        policy_version: int,
    ) -> dict[str, Any]:
        with self.host.connect() as connection:
            changed = connection.execute(
                """UPDATE host_thread_policy
                SET applied_revision=?, applied_policy_version=?, updated_at=unixepoch()
                WHERE organization_id=? AND agent_id=? AND session_id=? AND desired_revision=?""",
                (
                    expected_desired,
                    policy_version,
                    organization_id,
                    agent_id,
                    session_id,
                    expected_desired,
                ),
            ).rowcount
            if changed != 1:
                raise RuntimeUnavailable("Thread policy changed during native application")
        return self._policy_record(organization_id, agent_id, session_id)

    def _require_reply_admission(
        self, organization_id: str, agent_id: str, session_id: str
    ) -> None:
        agent = self.host.agent(organization_id, agent_id)
        if not agent["applied_envelope"] or agent["desired_envelope"] != agent["applied_envelope"]:
            raise RuntimeUnavailable("Interaction reply is waiting for agent configuration")
        envelope = HostAgentConfiguration.model_validate_json(agent["applied_envelope"])
        record = self._policy_record(organization_id, agent_id, session_id)
        if (
            record["desired_revision"] != record["applied_revision"]
            or record["applied_policy_version"] != envelope.policy_version
        ):
            raise RuntimeUnavailable("Interaction reply is waiting for thread policy")

    def _opencode_session(
        self, organization_id: str, agent_id: str, session_id: str
    ) -> dict[str, Any]:
        session = self.host.session(organization_id, agent_id, session_id)
        RuntimeRouter.require_supported(session["runtime_type"])
        return session

    def _applied_envelope(self, organization_id: str, agent_id: str) -> HostAgentConfiguration:
        agent = self.host.agent(organization_id, agent_id)
        if not agent["applied_envelope"]:
            raise RuntimeUnavailable("Agent configuration is not applied")
        return HostAgentConfiguration.model_validate_json(agent["applied_envelope"])

    async def _idle(
        self, organization_id: str, agent_id: str, session_id: str, directory: str
    ) -> bool:
        statuses = await self.runtime.request(
            organization_id, agent_id, "/session/status", directory=directory
        )
        if not isinstance(statuses, Mapping):
            raise RuntimeUnavailable("Native thread status response is invalid")
        if session_id not in statuses:
            return True
        state = statuses[session_id]
        if not isinstance(state, Mapping) or state.get("type") not in {"idle", "busy", "retry"}:
            raise RuntimeUnavailable("Native thread status response is invalid")
        return state["type"] == "idle"

    async def _pending_native(
        self,
        organization_id: str,
        agent_id: str,
        session_id: str,
        directory: str,
        kind: InteractionKind,
    ) -> list[dict[str, Any]]:
        result = await self.runtime.request(
            organization_id, agent_id, f"/{kind}", directory=directory
        )
        if not isinstance(result, list) or not all(isinstance(entry, dict) for entry in result):
            raise RuntimeUnavailable("Native pending interactions response is invalid")
        return [entry for entry in result if entry.get("sessionID") == session_id]


def _reply_request(
    kind: InteractionKind, request_id: str, answer: object
) -> tuple[dict[str, Any], str]:
    if kind == "question":
        if answer == {"reject": True}:
            return {}, f"/question/{request_id}/reject"
        if not (
            isinstance(answer, list)
            and all(isinstance(values, list) for values in answer)
            and all(isinstance(value, str) for values in answer for value in values)
        ):
            raise ValueError("Question answers must be nested strings")
        return {"answers": answer}, f"/question/{request_id}/reply"
    if answer not in {"once", "reject"}:
        raise ValueError("Permission replies must be once or reject")
    return {"reply": answer}, f"/permission/{request_id}/reply"


def _operation(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["answer"] = json.loads(result["answer"])
    result["author"] = json.loads(result["author"])
    return result


def _policy(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["overrides"] = [
        PermissionRule.model_validate(rule) for rule in json.loads(result["overrides"])
    ]
    result["author"] = json.loads(result["author"]) if result["author"] else None
    return result


def _policy_receipt(record: dict[str, Any], envelope: HostAgentConfiguration) -> dict[str, Any]:
    overrides = record["overrides"] if envelope.policy.allow_thread_overrides else []
    return {
        "session_id": record["session_id"],
        "desired_revision": record["desired_revision"],
        "applied_revision": record["applied_revision"],
        "applied_policy_version": record["applied_policy_version"],
        "rules": [rule.model_dump() for rule in overrides],
        "effective_rules": permission_rules(envelope, overrides),
        "author": record["author"],
        "updated_at": record["updated_at"],
    }


def _validated_native_id(value: object) -> str:
    try:
        return _native_id.validate_python(value)
    except ValidationError:
        raise RuntimeUnavailable("Native child session receipt is invalid") from None
