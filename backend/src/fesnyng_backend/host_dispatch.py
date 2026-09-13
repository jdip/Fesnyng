"""Durable delivery receipts around native OpenCode execution."""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import sqlite3
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import Field, model_validator

from fesnyng_backend.agent_models import Contract
from fesnyng_backend.host_effects import native_tools_settled
from fesnyng_backend.host_interactions import Interactions
from fesnyng_backend.host_models import Actor, HostAgentConfiguration
from fesnyng_backend.host_native_evidence import NativeEvidence
from fesnyng_backend.host_runtime import RuntimeUnavailable
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
        self, org: str, agent: str, session: str, submission: Submission, author: Actor
    ) -> dict[str, Any]:
        self.host.session(org, agent, session)
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
                tasks = [*self.tasks.values(), *self.probes.values()]
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
        groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for row in self.store.pending():
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
                configured = self.store.host.agent(org, agent)
                if (
                    not configured["applied_envelope"]
                    or configured["applied_envelope"] != configured["desired_envelope"]
                ):
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
                    not configured["applied_envelope"]
                    or configured["applied_envelope"] != configured["desired_envelope"]
                ):
                    raise RuntimeUnavailable("Agent configuration changed before native submission")
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
        except RuntimeUnavailable as error:
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

    async def _stop(self, row: dict[str, Any], session: dict[str, Any]) -> None:
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
        await self._settle_after_stop(row, session)
        current = self.store.get(org, agent, row["id"])
        self.store.change(
            current,
            "completed",
            outcome={"kind": "abort_acknowledged", "stop_id": row["id"]},
        )

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

    @staticmethod
    def _thread_busy(statuses: Any, session_id: str) -> bool:
        if not isinstance(statuses, dict):
            raise RuntimeUnavailable("Native thread status response is invalid")
        state = statuses.get(session_id, {})
        if not isinstance(state, dict) or not isinstance(state.get("type", "idle"), str):
            raise RuntimeUnavailable("Native thread status response is invalid")
        return state.get("type", "idle") != "idle"

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
