"""Durable delivery receipts around native OpenCode execution."""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import sqlite3
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import Field, TypeAdapter, ValidationError, model_validator

from fesnyng_backend.agent_models import Contract
from fesnyng_backend.codex_history import full_turns, is_unmaterialized
from fesnyng_backend.host_effects import native_tools_settled
from fesnyng_backend.host_interactions import Interactions
from fesnyng_backend.host_models import Actor, HostAgentConfiguration, NativeID
from fesnyng_backend.host_native_evidence import NativeEvidence
from fesnyng_backend.host_runtime import RuntimeRouter, RuntimeUnavailable, codex_thread_family
from fesnyng_backend.host_store import HostStore


class Submission(Contract):
    id: UUID
    text: str = Field(default="", max_length=200_000)
    command: str | None = Field(default=None, max_length=128, pattern=r"^[A-Za-z0-9_/-]+$")
    mode: Literal["queued", "steering", "stop"] = "queued"
    cancel_queued: bool = False
    origin_id: UUID | None = None

    @model_validator(mode="after")
    def require_content(self):
        if self.mode == "stop":
            if self.text or self.command:
                raise ValueError("Stop cannot include a prompt")
        elif self.cancel_queued:
            raise ValueError("Only an explicit stop can cancel queued work")
        elif not self.text.strip() and not self.command:
            raise ValueError("Submission requires text or a native command")
        return self


class HostSubmission(Submission):
    author: Actor


_native_id = TypeAdapter(NativeID)


class DispatchStore:
    def __init__(self, host: HostStore):
        self.host = host

    def initialize(self) -> None:
        with self.host.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS host_dispatches (
                    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL, session_id TEXT NOT NULL REFERENCES host_sessions(session_id),
                    sequence INTEGER NOT NULL, payload TEXT NOT NULL, author TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'queued', native_message_id TEXT,
                    receipt_validated INTEGER NOT NULL DEFAULT 0,
                    outcome TEXT, error TEXT, submitted_at REAL, created_at INTEGER NOT NULL DEFAULT (unixepoch()),
                    updated_at INTEGER NOT NULL DEFAULT (unixepoch()),
                    UNIQUE(session_id,sequence),
                    FOREIGN KEY(organization_id,agent_id) REFERENCES host_agents(organization_id,agent_id)
                );
            """)

    def enqueue(
        self,
        org: str,
        agent: str,
        session: str,
        submission: Submission,
        author: Actor,
        *,
        lifecycle_operation: bool = False,
    ) -> dict[str, Any]:
        mapped_session = self.host.session(org, agent, session)
        # The durable queue may admit either installed harness.  Native routing
        # happens only in Dispatcher._thread after it has the bound runtime;
        # never reject Codex here through the legacy OpenCode-only static gate.
        if mapped_session["runtime_type"] not in {"opencode", "codex"}:
            raise RuntimeUnavailable("Thread harness binding is invalid")
        payload = submission.model_dump_json()
        attribution = author.model_dump_json()
        with self.host.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT * FROM host_dispatches WHERE id=?", (str(submission.id),)
            ).fetchone()
            if previous:
                if (
                    previous["organization_id"],
                    previous["agent_id"],
                    previous["session_id"],
                    previous["payload"],
                    previous["author"],
                ) != (org, agent, session, payload, attribution):
                    raise ValueError("Delivery identity conflict")
                return self._record(previous)
            lifecycle = connection.execute(
                "SELECT lifecycle_state FROM host_agents WHERE organization_id=? AND agent_id=?",
                (org, agent),
            ).fetchone()
            if lifecycle is None:
                raise LookupError("Agent not found")
            if not lifecycle_operation and lifecycle["lifecycle_state"] in {
                "transitioning",
                "recovering",
                "recovery_required",
                "failed",
            }:
                raise RuntimeUnavailable("Agent lifecycle transition is holding new work")
            self.host.require_writable(org, agent, session, connection=connection)
            binding = connection.execute(
                """SELECT state FROM host_workspace_bindings
                WHERE organization_id=? AND agent_id=? AND session_id=?""",
                (org, agent, session),
            ).fetchone()
            if binding is not None and binding["state"] != "ready":
                raise RuntimeUnavailable("Workspace lifecycle operation is holding new work")
            sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 FROM host_dispatches WHERE session_id=?",
                (session,),
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO host_dispatches(id,organization_id,agent_id,session_id,sequence,payload,author) VALUES(?,?,?,?,?,?,?)",
                (str(submission.id), org, agent, session, sequence, payload, attribution),
            )
        return self.get(org, agent, str(submission.id))

    def get(self, org: str, agent: str, delivery_id: str) -> dict[str, Any]:
        with self.host.connect() as connection:
            row = connection.execute(
                "SELECT * FROM host_dispatches WHERE id=? AND organization_id=? AND agent_id=?",
                (delivery_id, org, agent),
            ).fetchone()
        if row is None:
            raise LookupError("Delivery not found")
        return self._record(row)

    def for_thread(self, org: str, agent: str, session: str) -> list[dict[str, Any]]:
        self.host.session(org, agent, session)
        with self.host.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM host_dispatches WHERE organization_id=? AND agent_id=? AND session_id=? ORDER BY sequence",
                (org, agent, session),
            ).fetchall()
        return [self._record(row) for row in rows]

    def latest_incoming_at(self, org: str, agent: str) -> dict[str, int]:
        """Return the newest admitted human or peer message for each mapped thread.

        Native OpenCode history identifies messages by role but cannot retain the
        Fesnyng actor who admitted them.  Dispatch receipts are therefore the
        provenance authority for workspace recency, including queued work that
        has not reached native execution yet.
        """
        with self.host.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM host_dispatches
                WHERE organization_id=? AND agent_id=? ORDER BY session_id,sequence""",
                (org, agent),
            ).fetchall()
        latest: dict[str, int] = {}
        for row in rows:
            receipt = self._record(row)
            if not self._is_incoming(receipt, agent):
                continue
            latest[receipt["session_id"]] = max(
                latest.get(receipt["session_id"], 0), receipt["created_at"] * 1000
            )
        return latest

    @staticmethod
    def _is_incoming(receipt: dict[str, Any], receiving_agent: str) -> bool:
        payload, author = receipt["payload"], receipt["author"]
        if not isinstance(payload, dict) or not isinstance(author, dict):
            return False
        if payload.get("mode") not in {"queued", "steering"}:
            return False
        if not isinstance(author.get("id"), str):
            return False
        return author.get("kind") == "human" or (
            author.get("kind") == "agent" and author["id"] != receiving_agent
        )

    def pending(self) -> list[dict[str, Any]]:
        with self.host.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM host_dispatches WHERE state NOT IN ('completed','failed','contributed','cancelled') ORDER BY session_id,sequence"
            ).fetchall()
        return [self._record(row) for row in rows]

    def change(
        self,
        record: dict[str, Any],
        state: str,
        *,
        message_id: str | None = None,
        validated: bool | None = None,
        outcome: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> bool:
        with self.host.connect() as connection:
            changed = connection.execute(
                "UPDATE host_dispatches SET state=?,native_message_id=COALESCE(?,native_message_id),receipt_validated=COALESCE(?,receipt_validated),outcome=?,error=?,submitted_at=CASE WHEN ? IS NOT NULL THEN ? ELSE submitted_at END,updated_at=unixepoch() WHERE id=? AND state=?",
                (
                    state,
                    message_id,
                    validated,
                    json.dumps(outcome) if outcome else None,
                    error,
                    message_id,
                    time.time(),
                    record["id"],
                    record["state"],
                ),
            ).rowcount
        return changed == 1

    @staticmethod
    def _record(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for field in ("payload", "author", "outcome"):
            result[field] = json.loads(result[field]) if result[field] else None
        result["receipt_validated"] = bool(result["receipt_validated"])
        return result


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


class Dispatcher:
    """Supervise durable deliveries; native OpenCode still owns every execution step."""

    def __init__(
        self, store: DispatchStore, runtime: NativeRuntime, interactions: Interactions | None = None
    ):
        self.store = store
        self.runtime = runtime
        self.interactions = interactions
        self.evidence = NativeEvidence(runtime)
        self.changed = asyncio.Event()
        self.tasks: dict[str, asyncio.Task] = {}
        self.probes: dict[tuple[str, str, str], asyncio.Task] = {}
        self.title_tasks: dict[tuple[str, str, str], asyncio.Task] = {}

    _STOP_SETTLE_SECONDS = 5.0

    def wake(self) -> None:
        self.changed.set()

    @asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        async with asyncio.TaskGroup() as group:
            supervisor = group.create_task(self._serve())
            try:
                yield
            finally:
                supervisor.cancel()
                tasks = [*self.tasks.values(), *self.probes.values(), *self.title_tasks.values()]
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _serve(self) -> None:
        while True:
            self.changed.clear()
            await self.step()
            try:
                await asyncio.wait_for(self.changed.wait(), timeout=0.5)
            except TimeoutError:
                pass

    async def step(self) -> None:
        if self.store.host.maintenance_status()["state"] == "closed":
            return
        for delivery_id, task in list(self.tasks.items()):
            if task.done():
                del self.tasks[delivery_id]
                if task.cancelled():
                    continue
                task.result()
        for key, probe in list(self.probes.items()):
            if probe.done():
                del self.probes[key]
                if not probe.cancelled():
                    probe.result()
        for key, task in list(self.title_tasks.items()):
            if task.done():
                del self.title_tasks[key]
                if not task.cancelled():
                    task.result()
        groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for row in self.store.pending():
            agent = self.store.host.agent(row["organization_id"], row["agent_id"])
            if agent["desired_state"] != "running" or agent["lifecycle_state"] != "running":
                continue
            if (
                self.store.host.session(row["organization_id"], row["agent_id"], row["session_id"])[
                    "frozen_at"
                ]
                is not None
            ):
                continue
            key = (row["organization_id"], row["agent_id"], row["session_id"])
            groups.setdefault(key, []).append(row)
        for key, rows in groups.items():
            if key not in self.probes:
                self.probes[key] = asyncio.create_task(self._probe(key, rows))
        await asyncio.sleep(0)

    async def _probe(self, key: tuple[str, str, str], rows: list[dict[str, Any]]) -> None:
        try:
            await self._thread(key, rows)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - isolate one malformed native thread probe.
            for row in rows:
                current = self.store.get(row["organization_id"], row["agent_id"], row["id"])
                self.store.change(current, current["state"], error=str(error))

    async def _thread(self, key: tuple[str, str, str], rows: list[dict[str, Any]]) -> None:
        org, agent, session_id = key
        session = self.store.host.session(org, agent, session_id)
        if session["frozen_at"] is not None:
            return
        if session["runtime_type"] == "codex":
            await self._codex_thread(key, rows, session)
            return
        RuntimeRouter.require_supported(session["runtime_type"])
        try:
            statuses = await self.runtime.request(
                org, agent, "/session/status", directory=session["directory"]
            )
            busy = self._thread_busy(statuses, session_id)
            history = await self.runtime.request(
                org, agent, f"/session/{session_id}/message", directory=session["directory"]
            )
            self._validate_history(history)
            for row in sorted(rows, key=lambda row: row["sequence"], reverse=True):
                if row["native_message_id"]:
                    await self._reconcile(row, history, busy, session["directory"])
            current = [self.store.get(org, agent, row["id"]) for row in rows]
            unsettled = [
                row
                for row in current
                if row["state"] not in {"completed", "failed", "contributed", "cancelled"}
            ]
            urgent = next(
                (
                    row
                    for row in unsettled
                    if row["state"] == "queued" and row["payload"]["mode"] == "stop"
                ),
                None,
            )
            stop_in_progress = any(
                row["payload"]["mode"] == "stop" and row["state"] in {"stopping", "unresolved"}
                for row in unsettled
            )
            steering = next(
                (
                    row
                    for row in unsettled
                    if row["state"] == "queued"
                    and row["payload"]["mode"] == "steering"
                    and self._can_admit_steering(row, unsettled)
                ),
                None,
            )
            row = (
                urgent
                or (None if stop_in_progress else steering)
                or (
                    unsettled[0]
                    if not stop_in_progress
                    and not busy
                    and unsettled
                    and unsettled[0]["state"] == "queued"
                    else None
                )
            )
            if row and row["id"] not in self.tasks:
                self.tasks[row["id"]] = asyncio.create_task(self._submit(row, session, history))
        except RuntimeUnavailable as error:
            for row in rows:
                self.store.change(row, row["state"], error=str(error))

    def _codex(self, session: Mapping[str, Any]) -> Any:
        """Resolve the bound App Server adapter without falling through to OpenCode."""
        router = getattr(self.runtime, "runtime_router", None)
        adapter = (
            router.for_session(session)
            if router is not None
            else getattr(self.runtime, "codex", None)
        )
        if adapter is None or not hasattr(adapter, "call"):
            raise RuntimeUnavailable("Codex harness is not available on this host")
        return adapter

    async def _codex_thread(
        self, key: tuple[str, str, str], rows: list[dict[str, Any]], session: dict[str, Any]
    ) -> None:
        """Reconcile Codex turns, whose receipts are native turn IDs rather than messages."""
        org, agent, session_id = key
        adapter = self._codex(session)
        try:
            turns = await full_turns(adapter, org, agent, session_id)
        except RuntimeUnavailable as error:
            # The pinned server cannot page an untouched thread.  It is safe
            # to start its first turn, but an already submitted turn remains
            # unresolved until the native record becomes inspectable.
            known_empty = all(
                row["state"] == "queued" and row["native_message_id"] is None for row in rows
            )
            if not known_empty or not is_unmaterialized(error, session_id):
                raise
            turns = []
        if not isinstance(turns, list) or not all(isinstance(turn, Mapping) for turn in turns):
            raise RuntimeUnavailable("Codex thread receipt is invalid")
        by_id = {turn.get("id"): turn for turn in turns if isinstance(turn.get("id"), str)}
        active_turns = {
            turn_id
            for turn_id, turn in by_id.items()
            if turn.get("status") not in {"completed", "failed", "interrupted"}
        }
        for row in sorted(rows, key=lambda item: item["sequence"], reverse=True):
            if row["native_message_id"] is None:
                correlated = next(
                    (
                        turn_id
                        for turn_id, turn in by_id.items()
                        if any(
                            isinstance(item, Mapping)
                            and item.get("type") == "userMessage"
                            and item.get("clientId") == row["id"]
                            for item in turn.get("items", [])
                            if isinstance(turn.get("items"), list)
                        )
                    ),
                    None,
                )
                if correlated is not None:
                    current = self.store.get(org, agent, row["id"])
                    self.store.change(current, "active", message_id=correlated, validated=True)
                    row = self.store.get(org, agent, row["id"])
            turn = by_id.get(row["native_message_id"])
            if turn is None:
                continue
            status = turn.get("status")
            if status in {"completed", "failed", "interrupted"}:
                current = self.store.get(org, agent, row["id"])
                # Interrupt acknowledgement is not terminal.  Once the
                # targeted turn reaches *any* terminal state, the requested
                # quieting action succeeded even when normal completion won
                # the race with the interrupt.
                stopped = row["payload"]["mode"] == "stop"
                self.store.change(
                    current,
                    "completed" if status == "completed" or stopped else "failed",
                    validated=True,
                    outcome={
                        "kind": "codex_interrupt_completed" if stopped else "codex_turn_completed",
                        "turn_id": row["native_message_id"],
                        "status": status,
                    },
                    error=None if status == "completed" or stopped else f"Codex turn {status}",
                )
            elif row["state"] == "submitting":
                self.store.change(row, "active", validated=True)
        current = [self.store.get(org, agent, row["id"]) for row in rows]
        # A receipt that was previously correlated but is absent from the
        # latest native history is an unknown effect, never evidence of idle.
        missing_active_receipt = any(
            row["state"] == "active"
            and isinstance(row["native_message_id"], str)
            and row["native_message_id"] not in by_id
            for row in current
        )
        # An earlier RPC can be in flight before App Server materializes its
        # turn.  Do not let a later queued prompt overtake that unknown effect.
        blocked_by_unknown = (
            any(
                row["state"] in {"submitting", "stopping", "uncertain", "unresolved"}
                for row in current
            )
            or missing_active_receipt
        )
        if blocked_by_unknown:
            # A stop may still quiet a separately known active native turn.
            # It never settles or bypasses the earlier unknown receipt, and
            # normal/steering submissions remain blocked below this point.
            urgent_stop = next(
                (
                    row
                    for row in current
                    if row["state"] == "queued" and row["payload"]["mode"] == "stop"
                ),
                None,
            )
            if urgent_stop is not None and active_turns and urgent_stop["id"] not in self.tasks:
                self.tasks[urgent_stop["id"]] = asyncio.create_task(
                    self._submit_codex(urgent_stop, session, next(iter(active_turns)))
                )
            return
        unsettled = [
            row
            for row in current
            if row["state"] not in {"completed", "failed", "contributed", "cancelled", "unresolved"}
        ]
        urgent = next(
            (
                row
                for row in unsettled
                if row["state"] == "queued" and row["payload"]["mode"] == "stop"
            ),
            None,
        )
        steering = next(
            (
                row
                for row in unsettled
                if row["state"] == "queued"
                and row["payload"]["mode"] == "steering"
                and active_turns
            ),
            None,
        )
        queued = next((row for row in unsettled if row["state"] == "queued"), None)
        candidate = urgent or steering or (queued if not active_turns else None)
        if candidate is not None and candidate["id"] not in self.tasks:
            self.tasks[candidate["id"]] = asyncio.create_task(
                self._submit_codex(candidate, session, next(iter(active_turns), None))
            )

    async def _submit_codex(
        self, row: dict[str, Any], session: dict[str, Any], active_turn_id: str | None
    ) -> None:
        """Submit exactly once.  An unavailable response is unresolved, never replayed."""
        org, agent, session_id = row["organization_id"], row["agent_id"], row["session_id"]
        try:
            adapter = self._codex(session)
            # Policy application takes the same per-agent lock.  Perform it
            # before admission serializes the native user turn, then recheck
            # the durable record while holding that lock.
            if row["payload"]["mode"] == "stop":
                self._require_codex_stop_admission(org, agent)
            else:
                await self._require_codex_admission(org, agent, session_id, apply_policy=True)
            async with self.runtime.lock(agent):
                self.store.host.require_writable(org, agent, session_id)
                if row["payload"]["mode"] == "stop":
                    self._require_codex_stop_admission(org, agent)
                else:
                    await self._require_codex_admission(org, agent, session_id, apply_policy=False)
                active_turn_id = await self._recheck_codex_head(
                    adapter, row, session, active_turn_id
                )
                if not self.store.change(row, "submitting"):
                    return
                current = self.store.get(org, agent, row["id"])
                if row["payload"]["mode"] == "stop":
                    if row["payload"]["cancel_queued"]:
                        for queued in self.store.for_thread(org, agent, session_id):
                            if (
                                queued["state"] == "queued"
                                and queued["sequence"] < row["sequence"]
                                and queued["payload"]["mode"] != "stop"
                            ):
                                self.store.change(
                                    self.store.get(org, agent, queued["id"]),
                                    "cancelled",
                                    outcome={
                                        "kind": "cancelled_before_submission",
                                        "stop_id": row["id"],
                                    },
                                )
                    if active_turn_id is None:
                        self.store.change(
                            current,
                            "completed",
                            validated=True,
                            outcome={"kind": "codex_interrupt_not_needed"},
                        )
                        return
                    reply = await adapter.call(
                        org,
                        agent,
                        "turn/interrupt",
                        {"threadId": session_id, "turnId": active_turn_id},
                    )
                    if not isinstance(reply, Mapping):
                        raise RuntimeUnavailable("Codex interrupt receipt is invalid")
                    # An RPC acknowledgement only says the interrupt was
                    # accepted.  Reconciliation observes the terminal native
                    # turn before completing this durable stop receipt.
                    self.store.change(
                        current,
                        "stopping",
                        message_id=active_turn_id,
                        outcome={"kind": "codex_interrupt_requested", "turn_id": active_turn_id},
                    )
                    return
                input_: list[dict[str, str]] = []
                command = row["payload"]["command"]
                if command:
                    input_.append(
                        await self._codex_skill_input(
                            adapter, org, agent, session["directory"], command
                        )
                    )
                if row["payload"]["text"]:
                    input_.append({"type": "text", "text": row["payload"]["text"]})
                if row["payload"]["mode"] == "steering":
                    if active_turn_id is None:
                        raise RuntimeUnavailable("Codex steering has no active turn")
                    reply = await adapter.call(
                        org,
                        agent,
                        "turn/steer",
                        {
                            "threadId": session_id,
                            "expectedTurnId": active_turn_id,
                            "input": input_,
                            "clientUserMessageId": row["id"],
                        },
                    )
                    turn_id = reply.get("turnId") if isinstance(reply, Mapping) else None
                else:
                    configuration = HostAgentConfiguration.model_validate_json(
                        self.store.host.agent(org, agent)["applied_envelope"]
                    ).configuration
                    turn = {
                        "threadId": session_id,
                        "input": input_,
                        "clientUserMessageId": row["id"],
                        "model": configuration.model,
                    }
                    # Null clears an earlier App Server override so a changed
                    # agent setting returns existing threads to model default.
                    turn["effort"] = configuration.reasoning_effort
                    reply = await adapter.call(
                        org,
                        agent,
                        "turn/start",
                        turn,
                    )
                    turn = reply.get("turn") if isinstance(reply, Mapping) else None
                    turn_id = turn.get("id") if isinstance(turn, Mapping) else None
                if not isinstance(turn_id, str):
                    raise RuntimeUnavailable("Codex turn receipt is invalid")
                current = self.store.get(org, agent, row["id"])
                self.store.change(current, "active", message_id=turn_id, validated=True)
                if row["payload"]["mode"] == "queued":
                    title_input = row["payload"]["text"].strip() or row["payload"]["command"]
                    if title_input:
                        self._start_codex_title(org, agent, session_id, title_input)
        except (RuntimeUnavailable, ValueError) as error:
            current = self.store.get(org, agent, row["id"])
            # Codex has no caller-selected native idempotency key.  Once a call
            # might have reached App Server, only an operator may resolve it.
            if current["state"] in {"submitting", "stopping"}:
                self.store.change(current, "unresolved", error=str(error))
            elif current["state"] == "queued":
                self.store.change(current, "queued", error=str(error))
        except asyncio.CancelledError:
            current = self.store.get(org, agent, row["id"])
            if current["state"] == "submitting":
                self.store.change(
                    current,
                    "unresolved",
                    error="Host stopped during Codex submission; inspect before repeating",
                )
            raise
        finally:
            self.wake()

    def _start_codex_title(self, org: str, agent: str, session_id: str, text: str) -> None:
        key = (org, agent, session_id)
        if key not in self.title_tasks and self.store.host.claim_title_generation(
            org, agent, session_id
        ):
            self.title_tasks[key] = asyncio.create_task(self._generate_codex_title(key, text))

    async def _generate_codex_title(self, key: tuple[str, str, str], text: str) -> None:
        org, agent, session_id = key
        try:
            session = self.store.host.session(org, agent, session_id)
            adapter = self._codex(session)
            title = await adapter.generate_thread_title(org, agent, text)
            if not isinstance(title, str) or not title:
                raise RuntimeUnavailable("Codex title generation receipt is invalid")
            title = title.strip()[:100]
            async with self.runtime.lock(agent):
                current = self.store.host.session(org, agent, session_id)
                if current["title_generation_state"] != "generating":
                    return
                await adapter.call(
                    org, agent, "thread/name/set", {"threadId": session_id, "name": title}
                )
                self.store.host.complete_title_generation(org, agent, session_id, title)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - title generation must not affect the user turn.
            self.store.host.fail_title_generation(org, agent, session_id)

    async def _require_codex_admission(
        self, organization_id: str, agent_id: str, session_id: str, *, apply_policy: bool
    ) -> None:
        """Require the host-applied Codex policy/configuration before a native turn."""
        agent = self.store.host.agent(organization_id, agent_id)
        if not agent["applied_envelope"]:
            raise RuntimeUnavailable("Agent configuration is pending")
        desired = HostAgentConfiguration.model_validate_json(agent["desired_envelope"])
        applied = HostAgentConfiguration.model_validate_json(agent["applied_envelope"])
        if desired != applied:
            raise RuntimeUnavailable("Agent configuration is pending")
        if applied.configuration.runtime_type != "codex":
            raise RuntimeUnavailable("Codex configuration is not applied")
        if self.interactions is not None:
            # A newly created thread receives the candidate policy in
            # `thread/start`, but its durable per-thread receipt is still
            # absent.  Apply through the policy owner before a user turn;
            # it verifies the native policy and only then marks the record.
            if apply_policy:
                await self.interactions.apply_policy(organization_id, agent_id, session_id)
            policy = self.interactions.get_policy(organization_id, agent_id, session_id)
            if (
                policy["desired_revision"] != policy["applied_revision"]
                or policy["applied_policy_version"] != applied.policy_version
            ):
                raise RuntimeUnavailable("Thread policy changed before native submission")

    def _require_codex_stop_admission(self, organization_id: str, agent_id: str) -> None:
        """Stopping remains available while a replacement configuration is staged."""
        agent = self.store.host.agent(organization_id, agent_id)
        applied = agent.get("applied_envelope")
        if not isinstance(applied, str):
            raise RuntimeUnavailable("Codex harness is not applied")
        if (
            HostAgentConfiguration.model_validate_json(applied).configuration.runtime_type
            != "codex"
        ):
            raise RuntimeUnavailable("Codex harness is not applied")

    async def _recheck_codex_head(
        self, adapter: Any, row: dict[str, Any], session: dict[str, Any], active_turn_id: str | None
    ) -> str | None:
        """Re-read durable/native ordering under the agent lock before an RPC."""
        org, agent, session_id = row["organization_id"], row["agent_id"], row["session_id"]
        latest = self.store.get(org, agent, row["id"])
        if latest["state"] != "queued":
            raise RuntimeUnavailable("Codex delivery is no longer queued")
        rows = self.store.for_thread(org, agent, session_id)
        earlier = [
            item
            for item in rows
            if item["sequence"] < latest["sequence"]
            and item["state"] not in {"completed", "failed", "contributed", "cancelled"}
        ]
        try:
            turns = await full_turns(adapter, org, agent, session_id)
        except RuntimeUnavailable as error:
            queued_cancellation = (
                latest["payload"]["mode"] == "stop"
                and latest["payload"]["cancel_queued"]
                and all(item["state"] == "queued" for item in earlier)
            )
            if (earlier and not queued_cancellation) or not is_unmaterialized(error, session_id):
                raise
            turns = []
        active = [
            turn["id"]
            for turn in turns
            if isinstance(turn, Mapping)
            and isinstance(turn.get("id"), str)
            and turn.get("status") not in {"completed", "failed", "interrupted"}
        ]
        if latest["payload"]["mode"] == "stop":
            if active:
                return active[0]
            if any(item["state"] != "queued" for item in earlier):
                raise RuntimeUnavailable("Codex stop is waiting for an earlier unknown delivery")
            return None
        if latest["payload"]["mode"] == "steering":
            # Steering is the one ordered submission that intentionally joins
            # the verified active turn.  It still cannot bypass an unknown or
            # uncorrelated earlier effect.
            if not active:
                raise RuntimeUnavailable("Codex steering has no active turn")
            if any(
                item["state"] != "queued"
                and (
                    item["state"] != "active"
                    or not isinstance(item["native_message_id"], str)
                    or item["native_message_id"] not in active
                )
                for item in earlier
            ):
                raise RuntimeUnavailable("Codex steering is waiting for earlier native work")
            return active[0]
        if earlier or active:
            raise RuntimeUnavailable("Codex delivery is waiting for earlier native work")
        return active_turn_id

    async def _codex_skill_input(
        self, adapter: Any, organization_id: str, agent_id: str, directory: str, command: str
    ) -> dict[str, str]:
        """Resolve one configured explicit skill from App Server immediately before use."""
        prefix, separator, name = command.partition("/")
        if prefix != "fesnyng" or not separator or not name:
            raise RuntimeUnavailable("Codex workflow command is not configured")
        envelope = HostAgentConfiguration.model_validate_json(
            self.store.host.agent(organization_id, agent_id)["applied_envelope"]
        )
        if name not in {
            skill.name for skill in envelope.configuration.skills if skill.explicit_only
        }:
            raise RuntimeUnavailable("Codex workflow command is not configured")
        reply = await adapter.call(
            organization_id, agent_id, "skills/list", {"cwds": [directory], "forceReload": True}
        )
        data = reply.get("data") if isinstance(reply, Mapping) else None
        if not isinstance(data, list):
            raise RuntimeUnavailable("Codex skills inventory receipt is invalid")
        matches: list[str] = []
        for entry in data:
            skills = entry.get("skills") if isinstance(entry, Mapping) else None
            if not isinstance(skills, list):
                raise RuntimeUnavailable("Codex skills inventory receipt is invalid")
            for skill in skills:
                if not isinstance(skill, Mapping):
                    raise RuntimeUnavailable("Codex skills inventory receipt is invalid")
                if skill.get("name") == name and isinstance(skill.get("path"), str):
                    matches.append(skill["path"])
        if len(set(matches)) != 1:
            raise RuntimeUnavailable("Configured Codex skill is not available natively")
        return {"type": "skill", "name": name, "path": matches[0]}

    async def _submit(
        self, row: dict[str, Any], session: dict[str, Any], history: list[dict[str, Any]]
    ) -> None:
        org, agent = row["organization_id"], row["agent_id"]
        try:
            if row["payload"]["mode"] == "stop":
                await self._stop(row, session)
                return
            payload = row["payload"]
            command: dict[str, Any] | None = None
            if payload["command"]:
                command = await self._native_command(
                    org, agent, payload["command"], session["directory"]
                )
                if command is None:
                    self.store.change(
                        row,
                        "failed",
                        outcome={
                            "kind": "native_command_unavailable",
                            "command": payload["command"],
                        },
                        error="Native command is not available",
                    )
                    return
                if self._thread_restricts_tasks(
                    org, agent, session["session_id"]
                ) and await self._command_starts_subtask(org, agent, command, session["directory"]):
                    self.store.change(
                        row,
                        "failed",
                        outcome={
                            "kind": "native_subtask_command_disallowed",
                            "command": payload["command"],
                        },
                        error="Native subtask command is disallowed by this thread policy",
                    )
                    return
                if self._thread_restricts_tasks(
                    org, agent, session["session_id"]
                ) and _command_executes_shell(command, payload["text"]):
                    self.store.change(
                        row,
                        "failed",
                        outcome={
                            "kind": "native_command_template_disallowed",
                            "command": payload["command"],
                        },
                        error="Native command template is disallowed by this thread policy",
                    )
                    return
            if self.interactions is not None:
                await self.interactions.apply_policy(org, agent, session["session_id"])
            async with self.runtime.lock(agent):
                self.store.host.require_writable(org, agent, session["session_id"])
                configured = self.store.host.agent(org, agent)
                if not configured["applied_envelope"] or HostAgentConfiguration.model_validate_json(
                    configured["applied_envelope"]
                ) != HostAgentConfiguration.model_validate_json(configured["desired_envelope"]):
                    raise RuntimeUnavailable("Agent configuration is pending")
                message_id = await _message_identifier(history)
                self._require_current_policy(org, agent, session["session_id"])
                if (
                    command
                    and self._thread_restricts_tasks(org, agent, session["session_id"])
                    and await self._command_starts_subtask(
                        org, agent, command, session["directory"]
                    )
                ):
                    self.store.change(
                        row,
                        "failed",
                        outcome={
                            "kind": "native_subtask_command_disallowed",
                            "command": payload["command"],
                        },
                        error="Native subtask command is disallowed by this thread policy",
                    )
                    return
                if (
                    command
                    and self._thread_restricts_tasks(org, agent, session["session_id"])
                    and _command_executes_shell(command, payload["text"])
                ):
                    self.store.change(
                        row,
                        "failed",
                        outcome={
                            "kind": "native_command_template_disallowed",
                            "command": payload["command"],
                        },
                        error="Native command template is disallowed by this thread policy",
                    )
                    return
                self._require_current_policy(org, agent, session["session_id"])
                configured = self.store.host.agent(org, agent)
                if (
                    configured["desired_state"] != "running"
                    or configured["lifecycle_state"] != "running"
                    or not configured["applied_envelope"]
                    or HostAgentConfiguration.model_validate_json(configured["applied_envelope"])
                    != HostAgentConfiguration.model_validate_json(configured["desired_envelope"])
                ):
                    raise RuntimeUnavailable(
                        "Agent lifecycle or configuration changed before native submission"
                    )
                envelope = HostAgentConfiguration.model_validate_json(
                    configured["applied_envelope"]
                )
                native_model = {
                    "providerID": envelope.configuration.provider,
                    "modelID": envelope.configuration.model,
                }
                if not self.store.change(row, "submitting", message_id=message_id):
                    return
            path = f"/session/{session['session_id']}/message"
            body = {
                "messageID": message_id,
                "parts": [{"type": "text", "text": payload["text"]}],
                "model": native_model,
                "system": (
                    f"Current thread workspace: {json.dumps(session['directory'])}. "
                    "Use this directory for this thread file work and tool workdir; parent-history "
                    "paths are historical unless the current task explicitly requires another location."
                ),
            }
            if payload["command"]:
                path = f"/session/{session['session_id']}/command"
                body = {
                    "messageID": message_id,
                    "command": payload["command"],
                    "arguments": payload["text"],
                    "model": f"{native_model['providerID']}/{native_model['modelID']}",
                }
            await self.runtime.request(
                org, agent, path, method="POST", body=body, directory=session["directory"]
            )
        except (RuntimeUnavailable, ValueError) as error:
            current = self.store.get(org, agent, row["id"])
            self.store.change(
                current,
                "unresolved"
                if current["state"] == "stopping"
                else "uncertain"
                if current["native_message_id"]
                else "queued",
                error=str(error),
            )
        except asyncio.CancelledError:
            current = self.store.get(org, agent, row["id"])
            if current["state"] in {"submitting", "stopping"}:
                self.store.change(
                    current,
                    "unresolved" if current["state"] == "stopping" else "uncertain",
                    error="Host stopped during native submission; awaiting reconciliation",
                )
            raise
        finally:
            self.wake()

    async def _stop(
        self,
        row: dict[str, Any],
        session: dict[str, Any],
        descendants: list[tuple[str, str]] | None = None,
    ) -> None:
        org, agent = row["organization_id"], row["agent_id"]
        if not self.store.change(row, "stopping"):
            return
        if row["payload"]["cancel_queued"]:
            for queued in self.store.for_thread(org, agent, row["session_id"]):
                if (
                    queued["state"] == "queued"
                    and queued["sequence"] < row["sequence"]
                    and queued["payload"]["mode"] != "stop"
                ):
                    current = self.store.get(org, agent, queued["id"])
                    if current["state"] == "queued":
                        self.store.change(
                            current,
                            "cancelled",
                            outcome={"kind": "cancelled_before_submission", "stop_id": row["id"]},
                        )
        acknowledged = await self.runtime.request(
            org,
            agent,
            f"/session/{row['session_id']}/abort",
            method="POST",
            body={},
            directory=session["directory"],
        )
        if acknowledged is not True:
            raise RuntimeUnavailable("Native abort has no verified receipt")
        for native_id, directory in reversed(descendants or []):
            acknowledged = await self.runtime.request(
                org,
                agent,
                f"/session/{native_id}/abort",
                method="POST",
                body={},
                directory=directory,
            )
            if acknowledged is not True:
                raise RuntimeUnavailable("Native abort has no verified receipt")
        await self._settle_after_stop(row, session)
        await self._settle_native_sessions(org, agent, descendants or [])
        current = self.store.get(org, agent, row["id"])
        self.store.change(
            current,
            "completed",
            outcome={"kind": "abort_acknowledged", "stop_id": row["id"]},
        )

    async def _settle_native_sessions(
        self, organization_id: str, agent_id: str, sessions: list[tuple[str, str]]
    ) -> None:
        for _ in range(30):
            if all(
                [
                    not self._thread_busy(
                        await self.runtime.request(
                            organization_id,
                            agent_id,
                            "/session/status",
                            directory=directory,
                        ),
                        native_id,
                    )
                    for native_id, directory in sessions
                ]
            ):
                return
            await asyncio.sleep(0.1)
        raise RuntimeUnavailable("Agent lifecycle change is waiting for native sessions to stop")

    def _earlier_tasks(self, stop: dict[str, Any]) -> list[asyncio.Task]:
        pending_deliveries = {delivery["id"]: delivery for delivery in self.store.pending()}
        earlier: list[asyncio.Task] = []
        for delivery_id, task in self.tasks.items():
            if delivery_id == stop["id"] or task.done():
                continue
            delivery = pending_deliveries.get(delivery_id)
            if delivery is None:
                continue
            if (
                delivery["session_id"] == stop["session_id"]
                and delivery["sequence"] < stop["sequence"]
                and delivery["payload"]["mode"] != "stop"
            ):
                earlier.append(task)
        return earlier

    async def _settle_after_stop(self, stop: dict[str, Any], session: dict[str, Any]) -> None:
        org, agent, session_id = stop["organization_id"], stop["agent_id"], stop["session_id"]
        deadline = asyncio.get_running_loop().time() + self._STOP_SETTLE_SECONDS
        while True:
            statuses = await self.runtime.request(
                org, agent, "/session/status", directory=session["directory"]
            )
            busy = self._thread_busy(statuses, session_id)
            history = await self.runtime.request(
                org, agent, f"/session/{session_id}/message", directory=session["directory"]
            )
            self._validate_history(history)
            earlier = [
                delivery
                for delivery in self.store.for_thread(org, agent, session_id)
                if delivery["sequence"] < stop["sequence"] and delivery["native_message_id"]
            ]
            for delivery in sorted(
                earlier, key=lambda delivery: delivery["sequence"], reverse=True
            ):
                await self._reconcile(delivery, history, busy, session["directory"])
            current = [self.store.get(org, agent, delivery["id"]) for delivery in earlier]
            verified_terminal = all(
                delivery["receipt_validated"]
                and delivery["state"] in {"completed", "failed", "contributed"}
                for delivery in current
            )
            tasks_pending = any(not task.done() for task in self._earlier_tasks(stop))
            if not busy and verified_terminal and not tasks_pending:
                return
            if busy:
                acknowledged = await self.runtime.request(
                    org,
                    agent,
                    f"/session/{session_id}/abort",
                    method="POST",
                    body={},
                    directory=session["directory"],
                )
                if acknowledged is not True:
                    raise RuntimeUnavailable("Native abort has no verified receipt")
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeUnavailable(
                    "Native stop has unverified earlier delivery effects; inspect before retrying"
                )
            await asyncio.sleep(0.05)

    def _require_current_policy(self, organization_id: str, agent_id: str, session_id: str) -> None:
        if self.interactions is None:
            return
        policy = self.interactions.get_policy(organization_id, agent_id, session_id)
        envelope = self.store.host.agent(organization_id, agent_id)["applied_envelope"]
        if not envelope:
            raise RuntimeUnavailable("Agent configuration is not applied")
        policy_version = HostAgentConfiguration.model_validate_json(envelope).policy_version
        if (
            policy["desired_revision"] != policy["applied_revision"]
            or policy["applied_policy_version"] != policy_version
        ):
            raise RuntimeUnavailable("Thread policy changed before native submission")

    async def _native_command(
        self, organization_id: str, agent_id: str, command: str, directory: str
    ) -> dict[str, Any] | None:
        commands = await self.runtime.request(
            organization_id, agent_id, "/command", directory=directory
        )
        if not isinstance(commands, list) or not all(isinstance(item, dict) for item in commands):
            raise RuntimeUnavailable("Native command inventory response is invalid")
        return next((item for item in commands if item.get("name") == command), None)

    def _thread_restricts_tasks(self, organization_id: str, agent_id: str, session_id: str) -> bool:
        if self.interactions is None:
            return False
        policy = self.interactions.get_policy(organization_id, agent_id, session_id)
        return any(rule.get("action") in {"ask", "deny"} for rule in policy["effective_rules"])

    async def _command_starts_subtask(
        self, organization_id: str, agent_id: str, command: dict[str, Any], directory: str
    ) -> bool:
        if command.get("subtask") is True:
            return True
        if command.get("subtask") is False:
            return False
        native_agent = command.get("agent")
        if native_agent is None:
            return False
        if not isinstance(native_agent, str):
            return True
        agents = await self.runtime.request(
            organization_id, agent_id, "/agent", directory=directory
        )
        if not isinstance(agents, list) or not all(isinstance(item, dict) for item in agents):
            return True
        selected = next((item for item in agents if item.get("name") == native_agent), None)
        if selected is None or not isinstance(selected.get("mode"), str):
            return True
        return selected["mode"] == "subagent"

    def policy_admissions_settled(
        self, organization_id: str, agent_id: str, session_id: str
    ) -> bool:
        deliveries = self.store.for_thread(organization_id, agent_id, session_id)
        for delivery in deliveries:
            resolved = (
                isinstance(delivery["outcome"], dict)
                and delivery["outcome"].get("kind") == "operator_resolution"
            )
            if delivery["native_message_id"] and (
                delivery["state"] not in {"completed", "failed", "contributed", "cancelled"}
                or (not delivery["receipt_validated"] and not resolved)
            ):
                return False
        active_ids = {delivery["id"] for delivery in deliveries if delivery["native_message_id"]}
        return not any(
            delivery_id in active_ids and not task.done()
            for delivery_id, task in self.tasks.items()
        )

    async def agent_active(self, organization_id: str, agent_id: str) -> bool:
        return bool(await self.agent_activity(organization_id, agent_id))

    async def agent_activity(self, organization_id: str, agent_id: str) -> str:
        active: list[str] = []
        active.extend(
            f"codex-title:{session_id}"
            for (org, agent, session_id), task in self.title_tasks.items()
            if (org, agent) == (organization_id, agent_id) and not task.done()
        )
        sessions = [
            session
            for session in self.store.host.sessions(organization_id, agent_id)
            if session["frozen_at"] is None
        ]
        codex_sessions = [session for session in sessions if session["runtime_type"] == "codex"]
        applied = self.store.host.agent(organization_id, agent_id).get("applied_envelope")
        zero_thread_codex = (
            not sessions
            and isinstance(applied, str)
            and HostAgentConfiguration.model_validate_json(applied).configuration.runtime_type
            == "codex"
        )
        if codex_sessions:
            statuses = getattr(self.runtime, "codex_thread_statuses", None)
            if statuses is None:
                raise RuntimeUnavailable(
                    "Codex thread status capability is unavailable on this host"
                )
            threads = await statuses(organization_id, agent_id)
            related = codex_thread_family(
                {session["session_id"] for session in codex_sessions}, threads
            )
            for thread in related:
                status = thread.get("status")
                kind = status.get("type") if isinstance(status, Mapping) else None
                if kind not in {"idle", "active", "notLoaded"}:
                    raise RuntimeUnavailable("Codex native thread status is uncertain")
                if kind == "active":
                    active.append(f"codex:{thread['id']}:active")
        directories = {
            session["directory"] for session in sessions if session["runtime_type"] == "opencode"
        }
        # A Codex-configured employee must never probe the OpenCode server,
        # including before it has created its first mapped thread.
        if directories or (not codex_sessions and not zero_thread_codex):
            for directory in [None, *sorted(directories)]:
                statuses = await self.runtime.request(
                    organization_id, agent_id, "/session/status", directory=directory
                )
                active.extend(
                    f"{directory or '<default>'}:{native_id}:{statuses[native_id]['type']}"
                    for native_id in self._active_native_ids(statuses)
                )
        return "|".join(sorted(set(active)))

    async def quiesce_agent(self, organization_id: str, agent_id: str, author: Actor) -> None:
        mapped = {
            session["session_id"]: session
            for session in self.store.host.sessions(organization_id, agent_id)
            if session["frozen_at"] is None
        }
        opencode = {
            session_id: session
            for session_id, session in mapped.items()
            if session["runtime_type"] == "opencode"
        }
        if opencode:
            families = {
                session_id: await self._session_family(organization_id, agent_id, session, opencode)
                for session_id, session in opencode.items()
            }
            known_ids = {native_id for family in families.values() for native_id, _ in family}
            for session_id, session in opencode.items():
                family = families[session_id]
                statuses_by_directory = {
                    directory: await self.runtime.request(
                        organization_id, agent_id, "/session/status", directory=directory
                    )
                    for directory in {directory for _, directory in family}
                }
                for statuses in statuses_by_directory.values():
                    if set(self._active_native_ids(statuses)) - known_ids:
                        raise RuntimeUnavailable(
                            "Active native session has no mapped Fesnyng thread"
                        )
                active = {
                    native_id
                    for native_id, directory in family
                    if self._thread_busy(statuses_by_directory[directory], native_id)
                }
                if not active:
                    continue
                stop = HostSubmission(
                    id=UUID(int=secrets.randbits(128)), mode="stop", author=author
                )
                receipt = self.store.enqueue(
                    organization_id, agent_id, session_id, stop, author, lifecycle_operation=True
                )
                await self._stop(receipt, session, family[1:])
            default_statuses = await self.runtime.request(
                organization_id, agent_id, "/session/status"
            )
            unowned = set(self._active_native_ids(default_statuses)) - known_ids
            if unowned:
                raise RuntimeUnavailable("Active native session has no mapped Fesnyng thread")
        elif not mapped and not (
            isinstance(
                self.store.host.agent(organization_id, agent_id).get("applied_envelope"), str
            )
            and HostAgentConfiguration.model_validate_json(
                self.store.host.agent(organization_id, agent_id)["applied_envelope"]
            ).configuration.runtime_type
            == "codex"
        ):
            default_statuses = await self.runtime.request(
                organization_id, agent_id, "/session/status"
            )
            if self._active_native_ids(default_statuses):
                raise RuntimeUnavailable("Active native session has no mapped Fesnyng thread")

        codex_sessions = [
            session for session in mapped.values() if session["runtime_type"] == "codex"
        ]
        codex_stops: list[dict[str, Any]] = []
        if codex_sessions:
            adapter = self._codex(codex_sessions[0])
            statuses = getattr(self.runtime, "codex_thread_statuses", None)
            if statuses is None:
                raise RuntimeUnavailable(
                    "Codex thread status capability is unavailable on this host"
                )
            threads = await statuses(organization_id, agent_id)
            roots = {session["session_id"] for session in codex_sessions}
            related = codex_thread_family(roots, threads)
            owner_for: dict[str, str] = {root: root for root in roots}
            changed = True
            while changed:
                changed = False
                for thread in related:
                    thread_id = thread["id"]
                    parent = thread.get("parentThreadId")
                    if parent in owner_for and thread_id not in owner_for:
                        owner_for[thread_id] = owner_for[parent]
                        changed = True
            if set(owner_for) != {thread["id"] for thread in related}:
                raise RuntimeUnavailable("Codex thread ancestry receipt is invalid")
            active_turns: dict[str, list[tuple[str, str]]] = {}
            for thread in related:
                thread_id = thread["id"]
                root = owner_for[thread_id]
                status = thread.get("status")
                kind = status.get("type") if isinstance(status, Mapping) else None
                if kind not in {"idle", "active", "notLoaded"}:
                    raise RuntimeUnavailable("Codex native thread status is uncertain")
                if kind != "active":
                    continue
                turns = await full_turns(adapter, organization_id, agent_id, thread_id)
                live = [
                    turn.get("id")
                    for turn in turns
                    if isinstance(turn, Mapping)
                    and isinstance(turn.get("id"), str)
                    and turn.get("status") not in {"completed", "failed", "interrupted"}
                ]
                if not live:
                    raise RuntimeUnavailable("Codex active thread has no interruptible turn")
                active_turns.setdefault(root, []).extend((thread_id, turn_id) for turn_id in live)
            for root, turns in active_turns.items():
                stop = HostSubmission(
                    id=UUID(int=secrets.randbits(128)), mode="stop", author=author
                )
                row = self.store.enqueue(
                    organization_id, agent_id, root, stop, author, lifecycle_operation=True
                )
                if not self.store.change(row, "stopping", message_id=turns[0][1]):
                    continue
                try:
                    for thread_id, turn_id in turns:
                        interrupt = await adapter.call(
                            organization_id,
                            agent_id,
                            "turn/interrupt",
                            {"threadId": thread_id, "turnId": turn_id},
                        )
                        if not isinstance(interrupt, Mapping):
                            raise RuntimeUnavailable("Codex interrupt receipt is invalid")
                except RuntimeUnavailable:
                    current = self.store.get(organization_id, agent_id, row["id"])
                    self.store.change(
                        current,
                        "unresolved",
                        error="Codex lifecycle interrupt needs reconciliation",
                    )
                    raise
                codex_stops.append(row)
        for _ in range(30):
            if not await self.agent_active(organization_id, agent_id):
                break
            await asyncio.sleep(0.1)
        else:
            raise RuntimeUnavailable(
                "Agent lifecycle change is waiting for native sessions to stop"
            )
        await self.reconcile_agent_effects(organization_id, agent_id)
        for row in codex_stops:
            current = self.store.get(organization_id, agent_id, row["id"])
            if current["state"] == "stopping":
                self.store.change(
                    current,
                    "completed",
                    validated=True,
                    outcome={
                        "kind": "codex_interrupt_completed",
                        "turn_id": current["native_message_id"],
                    },
                )
        if not self.agent_effects_settled(organization_id, agent_id):
            raise RuntimeUnavailable("Agent lifecycle change needs delivery reconciliation")

    async def _session_family(
        self,
        organization_id: str,
        agent_id: str,
        root: dict[str, Any],
        mapped: dict[str, dict[str, Any]],
    ) -> list[tuple[str, str]]:
        root_id, root_directory = root["session_id"], root["directory"]
        family = [(root_id, root_directory)]
        seen = {root_id}
        pending = [(root_id, root_directory)]
        while pending:
            parent_id, directory = pending.pop()
            children = await self.runtime.request(
                organization_id,
                agent_id,
                f"/session/{parent_id}/children",
                directory=directory,
            )
            if not isinstance(children, list):
                raise RuntimeUnavailable("Native child sessions response is invalid")
            for child in children:
                if not isinstance(child, Mapping):
                    raise RuntimeUnavailable("Native child session receipt is invalid")
                try:
                    child_id = _native_id.validate_python(child.get("id"))
                except ValidationError:
                    raise RuntimeUnavailable("Native child session receipt is invalid") from None
                child_directory = child.get("directory")
                if (
                    child.get("parentID") != parent_id
                    or child_id in seen
                    or not isinstance(child_directory, str)
                    or not child_directory
                ):
                    raise RuntimeUnavailable("Native child session ancestry is invalid")
                seen.add(child_id)
                if child_id in mapped:
                    if mapped[child_id]["directory"] != child_directory:
                        raise RuntimeUnavailable("Native child session ancestry is invalid")
                    continue
                family.append((child_id, child_directory))
                pending.append((child_id, child_directory))
        return family

    async def reconcile_agent_effects(self, organization_id: str, agent_id: str) -> None:
        async with self.runtime.lock(agent_id):
            rows = [
                row
                for row in self.store.pending()
                if row["organization_id"] == organization_id and row["agent_id"] == agent_id
            ]
            for session_id in {row["session_id"] for row in rows}:
                session = self.store.host.session(organization_id, agent_id, session_id)
                if session["frozen_at"] is not None:
                    continue
                session_rows = [row for row in rows if row["session_id"] == session_id]
                if session["runtime_type"] == "codex":
                    await self._codex_thread(
                        (organization_id, agent_id, session_id), session_rows, session
                    )
                    continue
                statuses = await self.runtime.request(
                    organization_id, agent_id, "/session/status", directory=session["directory"]
                )
                busy = self._thread_busy(statuses, session_id)
                history = await self.runtime.request(
                    organization_id,
                    agent_id,
                    f"/session/{session_id}/message",
                    directory=session["directory"],
                )
                self._validate_history(history)
                for row in session_rows:
                    if row["native_message_id"]:
                        await self._reconcile(row, history, busy, session["directory"])

    def agent_effects_settled(self, organization_id: str, agent_id: str) -> bool:
        unsettled = [
            row
            for row in self.store.pending()
            if row["organization_id"] == organization_id
            and row["agent_id"] == agent_id
            and row["state"] != "queued"
        ]
        active_ids = {row["id"] for row in unsettled}
        return not unsettled and not any(
            delivery_id in active_ids and not task.done()
            for delivery_id, task in self.tasks.items()
        )

    @staticmethod
    def _thread_busy(statuses: Any, session_id: str) -> bool:
        if not isinstance(statuses, dict):
            raise RuntimeUnavailable("Native thread status response is invalid")
        if session_id not in statuses:
            return False
        state = statuses[session_id]
        if not isinstance(state, dict) or state.get("type") not in {"idle", "busy", "retry"}:
            raise RuntimeUnavailable("Native thread status response is invalid")
        return state["type"] != "idle"

    @staticmethod
    def _active_native_ids(statuses: Any) -> list[str]:
        if not isinstance(statuses, dict):
            raise RuntimeUnavailable("Native thread status response is invalid")
        active: list[str] = []
        for native_id, state in statuses.items():
            if (
                not isinstance(native_id, str)
                or not isinstance(state, dict)
                or state.get("type") not in {"idle", "busy", "retry"}
            ):
                raise RuntimeUnavailable("Native thread status response is invalid")
            if state["type"] != "idle":
                active.append(native_id)
        return active

    @staticmethod
    def _validate_history(history: Any) -> None:
        if not isinstance(history, list):
            raise RuntimeUnavailable("Native message history response is invalid")
        for message in history:
            if not isinstance(message, dict):
                raise RuntimeUnavailable("Native message history response is invalid")
            info = message.get("info")
            if (
                not isinstance(info, dict)
                or not isinstance(info.get("id"), str)
                or not isinstance(info.get("sessionID"), str)
                or not isinstance(message.get("parts"), list)
                or not all(isinstance(part, dict) for part in message["parts"])
                or ("time" in info and not isinstance(info["time"], dict))
            ):
                raise RuntimeUnavailable("Native message history response is invalid")

    @staticmethod
    def _can_admit_steering(row: dict[str, Any], deliveries: list[dict[str, Any]]) -> bool:
        return not any(
            earlier["sequence"] < row["sequence"]
            and earlier["payload"]["mode"] != "stop"
            and earlier["state"] not in {"completed", "failed", "contributed", "cancelled"}
            and not (earlier["state"] == "active" and earlier["receipt_validated"])
            for earlier in deliveries
        )

    async def _reconcile(
        self, row: dict[str, Any], history: list[dict[str, Any]], busy: bool, directory: str
    ) -> None:
        message = next(
            (message for message in history if message["info"]["id"] == row["native_message_id"]),
            None,
        )
        validated = bool(
            message
            and message["info"].get("role") == "user"
            and message["info"].get("sessionID") == row["session_id"]
            and message.get("parts")
        )
        if validated and not row["payload"]["command"]:
            validated = any(
                part.get("type") == "text" and part.get("text") == row["payload"]["text"]
                for part in message["parts"]
            )
        if validated:
            replies = [
                message
                for message in history
                if message["info"].get("parentID") == row["native_message_id"]
                and message["info"].get("sessionID") == row["session_id"]
            ]
            tools_finished = native_tools_settled(
                replies
            ) and await self.evidence.delegated_tools_settled(
                row["organization_id"], row["agent_id"], row["session_id"], directory, replies
            )
            for reply in reversed(replies):
                info = reply["info"]
                if (
                    tools_finished
                    and info.get("time", {}).get("completed")
                    and (
                        info.get("error")
                        or info.get("finish") in {"stop", "length", "content-filter"}
                    )
                ):
                    self.store.change(
                        row,
                        "failed" if info.get("error") else "completed",
                        validated=True,
                        outcome={
                            "kind": "native_error" if info.get("error") else "native_run_completed",
                            "message_id": info["id"],
                            "responding_to": row["native_message_id"],
                        },
                    )
                    return
            if busy:
                self.store.change(row, "active", validated=True)
                return
            related = {
                item["native_message_id"]: item
                for item in self.store.for_thread(
                    row["organization_id"], row["agent_id"], row["session_id"]
                )
                if item["native_message_id"]
            }
            for reply in reversed(history):
                info = reply["info"]
                contributor = related.get(info.get("parentID"))
                if (
                    tools_finished
                    and contributor
                    and contributor["sequence"] > row["sequence"]
                    and contributor["payload"]["mode"] == "steering"
                    and contributor["receipt_validated"]
                    and contributor["state"] in {"completed", "failed"}
                ):
                    self.store.change(
                        row,
                        "contributed",
                        validated=True,
                        outcome={
                            "kind": "native_run_contribution",
                            "message_id": info["id"],
                            "responding_to": info["parentID"],
                        },
                    )
                    return
        if (
            not busy
            and row["id"] not in self.tasks
            and time.time() - (row["submitted_at"] or time.time()) > 5
        ):
            self.store.change(
                row,
                "unresolved",
                validated=validated,
                error="Native execution has no verified outcome; inspect before repeating",
            )


async def _message_identifier(history: list[dict[str, Any]]) -> str:
    # Match pinned OpenCode's 48-bit millisecond/counter encoding and base62 suffix.
    recent = [
        int(message["info"]["id"][4:16], 16) >> 12
        for message in history
        if re.fullmatch(r"msg_[0-9a-f]{12}[A-Za-z0-9]{14}", message["info"]["id"])
    ]
    for _ in range(500):
        now = int(time.time() * 1000)
        if not recent or (now & ((1 << 36) - 1)) > max(recent):
            encoded = ((now << 12) + 1) & ((1 << 48) - 1)
            identifier = f"msg_{encoded:012x}" + "".join(
                secrets.choice("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
                for _ in range(14)
            )
            await asyncio.sleep(0.002)
            return identifier
        await asyncio.sleep(0.002)
    raise RuntimeUnavailable("Host and native message clocks need synchronization")


def _command_executes_shell(command: dict[str, Any], arguments: str) -> bool:
    template = command.get("template")
    if not isinstance(template, str):
        return False
    # Pinned OpenCode expands positional arguments, then $ARGUMENTS, before
    # ConfigMarkdown.shell executes substitutions outside prompt permissions.
    raw = re.findall(r"""(?:\[Image\s+\d+\]|"[^"]*"|'[^']*'|[^\s"']+)""", arguments, re.IGNORECASE)
    args = [re.sub(r"""^["']|["']$""", "", value) for value in raw]
    positions = [int(value) for value in re.findall(r"\$([0-9]+)", template)]
    last = max(positions, default=0)

    def positional(match: re.Match[str]) -> str:
        position = int(match[1])
        if position > len(args):
            return ""
        if position == last:
            return " ".join(args[position - 1 :])
        return args[position - 1] if position else "undefined"

    with_args = re.sub(r"\$([0-9]+)", positional, template)

    def all_arguments(match: re.Match[str]) -> str:
        # Native replaceAll uses a replacement string, including JS dollar tokens.
        replacements = {
            "$": "$",
            "&": match[0],
            "`": with_args[: match.start()],
            "'": with_args[match.end() :],
        }
        return re.sub(r"\$([$&`'])", lambda token: replacements[token[1]], arguments)

    rendered = re.sub(r"\$ARGUMENTS", all_arguments, with_args)
    if not positions and "$ARGUMENTS" not in template and arguments.strip():
        rendered += f"\n\n{arguments}"
    return bool(re.search(r"!`([^`]+)`", rendered))
