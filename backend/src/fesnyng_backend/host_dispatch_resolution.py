"""Attributed resolution of native dispatches with unknown outcomes."""

from __future__ import annotations

import json
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import Field, model_validator

from fesnyng_backend.agent_models import Contract
from fesnyng_backend.host_dispatch import DispatchStore
from fesnyng_backend.host_models import Actor, NativeID
from fesnyng_backend.host_native_evidence import NativeEvidence
from fesnyng_backend.host_runtime import RuntimeUnavailable
from fesnyng_backend.host_store import HostStore


class DispatchResolution(Contract):
    """A stable operator decision about an otherwise unverifiable delivery."""

    operation_id: UUID
    outcome: Literal["completed", "failed"]
    evidence: str = Field(min_length=1, max_length=200_000)

    @model_validator(mode="after")
    def require_meaningful_evidence(self):
        if not self.evidence.strip():
            raise ValueError("Resolution evidence is required")
        return self


class HostDispatchResolution(DispatchResolution):
    author: Actor


class NativeRuntime(Protocol):
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


class DispatchResolutionService:
    """Close only a stopped native delivery after an accountable investigation."""

    _RESOLVABLE_STATES = frozenset({"unresolved", "uncertain", "stopping", "submitting"})

    def __init__(
        self, host: HostStore, dispatches: DispatchStore, dispatcher: Any, runtime: NativeRuntime
    ):
        self.host = host
        self.dispatches = dispatches
        self.dispatcher = dispatcher
        self.runtime = runtime

    async def resolve(
        self,
        organization_id: str,
        agent_id: str,
        session_id: NativeID,
        delivery_id: UUID,
        resolution: HostDispatchResolution,
    ) -> dict[str, Any]:
        """Record one non-replaying resolution once native activity is demonstrably idle."""

        receipt = self.dispatches.get(organization_id, agent_id, str(delivery_id))
        if receipt["session_id"] != session_id:
            raise LookupError("Delivery not found")
        existing = _operator_resolution(receipt.get("outcome"))
        if existing is not None:
            _same_resolution(existing, resolution)
            return receipt
        if receipt["state"] not in self._RESOLVABLE_STATES:
            raise ValueError("Delivery is not eligible for operator resolution")

        task = self.dispatcher.tasks.get(str(delivery_id))
        if task is not None and not task.done():
            raise ValueError("Delivery still has a local submission task")

        session = self.host.session(organization_id, agent_id, session_id)
        statuses = await self.runtime.request(
            organization_id, agent_id, "/session/status", directory=session["directory"]
        )
        if not isinstance(statuses, dict):
            raise RuntimeUnavailable("Native runtime returned invalid status")
        status = statuses.get(session_id, {})
        if not isinstance(status, dict) or status.get("type", "idle") != "idle":
            raise ValueError("Native session is not idle")
        history = await self.runtime.request(
            organization_id,
            agent_id,
            f"/session/{session_id}/message",
            directory=session["directory"],
        )
        if not isinstance(history, list):
            raise RuntimeUnavailable("Native runtime returned invalid message history")
        if _has_running_tool(history):
            raise ValueError("Native session still has a running tool")

        if not await NativeEvidence(self.runtime).delegated_tools_quiet(
            organization_id, agent_id, session_id, session["directory"], history
        ):
            raise ValueError("Native descendants are not verified quiet")
        return self._persist(organization_id, agent_id, session_id, str(delivery_id), resolution)

    def _persist(
        self,
        organization_id: str,
        agent_id: str,
        session_id: str,
        delivery_id: str,
        resolution: HostDispatchResolution,
    ) -> dict[str, Any]:
        with self.host.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM host_dispatches WHERE id=? AND organization_id=? AND agent_id=?",
                (delivery_id, organization_id, agent_id),
            ).fetchone()
            if row is None or row["session_id"] != session_id:
                raise LookupError("Delivery not found")
            receipt = self.dispatches._record(row)
            existing = _operator_resolution(receipt.get("outcome"))
            if existing is not None:
                _same_resolution(existing, resolution)
                return receipt
            if receipt["state"] not in self._RESOLVABLE_STATES:
                raise ValueError("Delivery is not eligible for operator resolution")
            outcome = {
                "kind": "operator_resolution",
                "operation_id": str(resolution.operation_id),
                "outcome": resolution.outcome,
                "evidence": resolution.evidence,
                "author": resolution.author.model_dump(mode="json"),
                "unknown_native_outcome": {
                    "state": receipt["state"],
                    "native_message_id": receipt["native_message_id"],
                    "receipt_validated": receipt["receipt_validated"],
                    "outcome": receipt["outcome"],
                    "error": receipt["error"],
                },
            }
            changed = connection.execute(
                "UPDATE host_dispatches SET state=?,outcome=?,error=NULL,updated_at=unixepoch() "
                "WHERE id=? AND organization_id=? AND agent_id=? AND session_id=? AND state=?",
                (
                    resolution.outcome,
                    json.dumps(outcome),
                    delivery_id,
                    organization_id,
                    agent_id,
                    session_id,
                    receipt["state"],
                ),
            ).rowcount
            if changed != 1:
                raise ValueError("Delivery changed during operator resolution")
        return self.dispatches.get(organization_id, agent_id, delivery_id)


def _operator_resolution(outcome: Any) -> dict[str, Any] | None:
    return (
        outcome
        if isinstance(outcome, dict) and outcome.get("kind") == "operator_resolution"
        else None
    )


def _same_resolution(existing: dict[str, Any], resolution: HostDispatchResolution) -> None:
    candidate = {
        "operation_id": str(resolution.operation_id),
        "outcome": resolution.outcome,
        "evidence": resolution.evidence,
        "author": resolution.author.model_dump(mode="json"),
    }
    if any(existing.get(key) != value for key, value in candidate.items()):
        raise ValueError("Operator resolution identity conflict")


def _has_running_tool(history: list[Any]) -> bool:
    for message in history:
        if not isinstance(message, dict):
            raise RuntimeUnavailable("Native runtime returned invalid message history")
        parts = message.get("parts", [])
        if not isinstance(parts, list):
            raise RuntimeUnavailable("Native runtime returned invalid message history")
        for part in parts:
            if not isinstance(part, dict) or part.get("type") != "tool":
                continue
            state = part.get("state")
            if not isinstance(state, dict) or state.get("status") not in {"completed", "error"}:
                return True
    return False
