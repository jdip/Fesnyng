"""Durable peer-to-peer delivery receipts over host-authenticated transport."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
from pydantic import Field

from fesnyng_backend.agent_models import Contract, Name, Slug
from fesnyng_backend.host_dispatch import DispatchStore, Submission
from fesnyng_backend.host_models import Actor, NativeID
from fesnyng_backend.host_runtime import RuntimeUnavailable
from fesnyng_backend.host_store import HostStore
from fesnyng_backend.peer_configuration import PeerConfigurationStore


class PeerSend(Contract):
    id: UUID
    target_agent: UUID
    target_session: NativeID | None = None
    text: str = Field(min_length=1, max_length=200_000)
    mode: Literal["queued", "steering"] = "queued"
    workspace: Slug = "default"
    title: Name = "Peer collaboration"
    origin_id: UUID | None = None


class PeerEnvelope(PeerSend):
    organization_id: UUID
    source_host: UUID
    source_agent: UUID
    source_session: NativeID
    kind: Literal["message", "result"] = "message"


class NativeRuntime(Protocol):
    async def create_session(
        self,
        organization_id: str,
        agent_id: str,
        title: str,
        workspace: str,
        *,
        directory: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> dict[str, Any]: ...

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


Transport = Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]]


class DispatchWaker(Protocol):
    def wake(self) -> None: ...


class PeerDeliveryService:
    def __init__(
        self,
        store: HostStore,
        config: PeerConfigurationStore,
        runtime: NativeRuntime,
        dispatch: DispatchStore,
        dispatcher: DispatchWaker,
        transport: Transport | None = None,
    ) -> None:
        self.store = store
        self.config = config
        self.runtime = runtime
        self.dispatch = dispatch
        self.dispatcher = dispatcher
        self.transport = transport or _post_envelope
        self.changed = asyncio.Event()
        self.inbound_locks: dict[str, asyncio.Lock] = {}
        self.delivery_locks: dict[str, asyncio.Lock] = {}
        self.delivery_tasks: dict[str, asyncio.Task[None]] = {}
        self.result_tasks: dict[str, asyncio.Task[None]] = {}

    def initialize(self) -> None:
        with self.store.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS peer_outbox (
                    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, source_agent TEXT NOT NULL,
                    source_session TEXT NOT NULL, target_host TEXT NOT NULL, envelope TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending', receipt TEXT, error TEXT, result_id TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL DEFAULT (unixepoch()), updated_at INTEGER NOT NULL DEFAULT (unixepoch())
                );
                CREATE TABLE IF NOT EXISTS peer_inbox (
                    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, source_host TEXT NOT NULL,
                    source_agent TEXT NOT NULL, source_session TEXT NOT NULL, target_agent TEXT NOT NULL,
                    target_session TEXT, directory TEXT, envelope TEXT NOT NULL, author TEXT, dispatch_id TEXT,
                    result_id TEXT, state TEXT NOT NULL DEFAULT 'reserved', error TEXT,
                    created_at INTEGER NOT NULL DEFAULT (unixepoch()), updated_at INTEGER NOT NULL DEFAULT (unixepoch())
                );
                """
            )
            connection.execute("BEGIN IMMEDIATE")
            outbox_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(peer_outbox)")
            }
            if "result_id" not in outbox_columns:
                connection.execute("ALTER TABLE peer_outbox ADD COLUMN result_id TEXT")
                connection.execute(
                    "UPDATE peer_outbox SET attempts=1 WHERE state='pending' AND attempts=0"
                )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(peer_inbox)")}
            if "author" not in columns:
                connection.execute("ALTER TABLE peer_inbox ADD COLUMN author TEXT")

    async def send(
        self, organization_id: str, source_agent: str, source_session: str, body: PeerSend
    ) -> dict[str, Any]:
        self.store.session(organization_id, source_agent, source_session)
        target = self.config.agent(organization_id, str(body.target_agent))
        envelope = PeerEnvelope(
            **body.model_dump(),
            organization_id=organization_id,
            source_host=self.store.instance_id,
            source_agent=source_agent,
            source_session=source_session,
        )
        encoded = envelope.model_dump_json()
        existing_delivery = False
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM peer_outbox WHERE id=?", (str(body.id),)
            ).fetchone()
            if existing is not None:
                if (
                    existing["organization_id"] != organization_id
                    or existing["envelope"] != encoded
                ):
                    raise ValueError("Peer delivery identity conflict")
                existing_delivery = True
            else:
                connection.execute(
                    "INSERT INTO peer_outbox(id,organization_id,source_agent,source_session,target_host,envelope) VALUES(?,?,?,?,?,?)",
                    (
                        str(body.id),
                        organization_id,
                        source_agent,
                        source_session,
                        target["host_id"],
                        encoded,
                    ),
                )
        if existing_delivery:
            self._publish_terminal_notices()
            return self.status(organization_id, source_agent, str(body.id))
        receipt = self.status(organization_id, source_agent, str(body.id))
        await self._deliver_with_timeout(receipt)
        return self.status(organization_id, source_agent, str(body.id))

    async def receive(self, source_host: str, envelope: PeerEnvelope) -> dict[str, Any]:
        lock = self.inbound_locks.setdefault(str(envelope.id), asyncio.Lock())
        async with lock:
            return await self._receive(source_host, envelope)

    async def _receive(self, source_host: str, envelope: PeerEnvelope) -> dict[str, Any]:
        org, delivery_id = str(envelope.organization_id), str(envelope.id)
        if str(envelope.source_host) != source_host:
            raise PermissionError("Peer source host mismatch")
        source = self.config.agent(org, str(envelope.source_agent))
        if source["host_id"] != source_host:
            raise PermissionError("Peer source agent is not rostered to this host")
        target = self.config.agent(org, str(envelope.target_agent))
        if target["host_id"] != str(self.store.instance_id):
            raise PermissionError("Peer target agent is not hosted here")
        encoded = envelope.model_dump_json()
        author = Actor(
            kind="agent",
            id=envelope.source_agent,
            name=source["name"],
            session_id=envelope.source_session,
        )
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM peer_inbox WHERE id=?", (delivery_id,)
            ).fetchone()
            if existing is not None:
                if existing["organization_id"] != org or existing["envelope"] != encoded:
                    raise ValueError("Peer delivery identity conflict")
                inbox = dict(existing)
            else:
                directory = None if envelope.target_session else _directory(envelope)
                connection.execute(
                    "INSERT INTO peer_inbox(id,organization_id,source_host,source_agent,source_session,"
                    "target_agent,target_session,directory,envelope,author) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        delivery_id,
                        org,
                        source_host,
                        str(envelope.source_agent),
                        envelope.source_session,
                        str(envelope.target_agent),
                        envelope.target_session,
                        directory,
                        encoded,
                        author.model_dump_json(),
                    ),
                )
                inbox = None
        if inbox is None:
            inbox = self._inbox(delivery_id)
        if inbox["state"] == "accepted":
            return self._inbox_receipt(inbox)
        return await self._recover_inbox(inbox, envelope, source)

    async def _recover_inbox(
        self, inbox: dict[str, Any], envelope: PeerEnvelope, source: dict[str, Any]
    ) -> dict[str, Any]:
        """Resume a durable reservation without ever blindly recreating native state."""
        session_id = inbox["target_session"]
        if session_id is None:
            if inbox["state"] == "reserved":
                # The reservation is durable before creation begins.  Only this
                # state proves no native create was attempted.
                with self.store.connect() as connection:
                    connection.execute(
                        "UPDATE peer_inbox SET state='creating',updated_at=unixepoch() WHERE id=? AND state='reserved'",
                        (inbox["id"],),
                    )
                inbox = self._inbox(inbox["id"])
                session_id = await self._reserve_session(inbox, envelope)
            else:
                try:
                    adopted = await self._adopt_session(inbox)
                except RuntimeUnavailable:
                    adopted = self._local_session_match(inbox)
                if adopted is None:
                    with self.store.connect() as connection:
                        connection.execute(
                            "UPDATE peer_inbox SET state='uncertain',error='Native session creation outcome unknown',updated_at=unixepoch() WHERE id=?",
                            (inbox["id"],),
                        )
                    return self._inbox_receipt(self._inbox(inbox["id"]))
                session_id = self._save_or_validate_session(inbox, adopted, envelope.title)
        return self._enqueue_inbox(inbox, envelope, source, session_id)

    def _enqueue_inbox(
        self,
        inbox: dict[str, Any],
        envelope: PeerEnvelope,
        source: dict[str, Any],
        session_id: str,
    ) -> dict[str, Any]:
        """DispatchStore provides the second idempotency boundary after recovery."""
        org, delivery_id = inbox["organization_id"], inbox["id"]
        author = (
            Actor.model_validate_json(inbox["author"])
            if inbox["author"]
            else Actor(
                kind="agent",
                id=envelope.source_agent,
                name=source["name"],
                session_id=envelope.source_session,
            )
        )
        delivery = self.dispatch.enqueue(
            org,
            str(envelope.target_agent),
            session_id,
            Submission(
                id=envelope.id,
                text=envelope.text,
                mode=envelope.mode,
                origin_id=envelope.origin_id,
            ),
            author,
        )
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE peer_inbox SET target_session=?,dispatch_id=?,state='accepted',error=NULL,updated_at=unixepoch() WHERE id=?",
                (session_id, delivery["id"], delivery_id),
            )
        self.dispatcher.wake()
        return self._inbox_receipt(self._inbox(delivery_id))

    def status(self, organization_id: str, source_agent: str, delivery_id: str) -> dict[str, Any]:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM peer_outbox WHERE id=? AND organization_id=? AND source_agent=?",
                (delivery_id, organization_id, source_agent),
            ).fetchone()
        if row is None:
            raise LookupError("Peer delivery not found")
        return _outbox(row)

    def received(self, organization_id: str, source_host: str, delivery_id: str) -> dict[str, Any]:
        """Return a receiver receipt only to the host that originated it."""
        row = self._inbox(delivery_id)
        if row["organization_id"] != organization_id or row["source_host"] != source_host:
            raise PermissionError("Peer delivery belongs to another host")
        receipt = self._inbox_receipt(row)
        if row["dispatch_id"]:
            delivery = self.dispatch.get(
                row["organization_id"], row["target_agent"], row["dispatch_id"]
            )
            receipt["outcome_state"] = delivery["state"]
            receipt["outcome"] = delivery["outcome"]
            receipt["outcome_error"] = delivery["error"]
        return receipt

    @asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        task = asyncio.create_task(self._serve())
        try:
            yield
        finally:
            task.cancel()
            tasks = [task, *self.delivery_tasks.values(), *self.result_tasks.values()]
            for pending in tasks:
                pending.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.delivery_tasks.clear()
            self.result_tasks.clear()

    async def _serve(self) -> None:
        while True:
            self.changed.clear()
            for delivery_id, task in list(self.delivery_tasks.items()):
                if not task.done():
                    continue
                del self.delivery_tasks[delivery_id]
                if task.cancelled():
                    continue
                try:
                    task.result()
                except Exception as error:  # noqa: BLE001 - retain isolated operation failure.
                    self._record_delivery_failure(delivery_id, error)
            for inbox_id, task in list(self.result_tasks.items()):
                if not task.done():
                    continue
                del self.result_tasks[inbox_id]
                if task.cancelled():
                    continue
                try:
                    task.result()
                except Exception as error:  # noqa: BLE001 - retain result uncertainty for inspection.
                    self._record_result_uncertainty(inbox_id, error)
            with self.store.connect() as connection:
                rows = connection.execute(
                    "SELECT * FROM peer_outbox WHERE state='pending' AND next_attempt<=?",
                    (time.time(),),
                ).fetchall()
            for row in rows:
                receipt = _outbox(row)
                if receipt["id"] not in self.delivery_tasks:
                    self.delivery_tasks[receipt["id"]] = asyncio.create_task(
                        self._deliver_with_timeout(receipt)
                    )
            self._publish_results()
            self._publish_terminal_notices()
            try:
                await asyncio.wait_for(self.changed.wait(), timeout=1)
            except TimeoutError:
                pass

    async def _deliver(self, receipt: dict[str, Any]) -> None:
        if receipt["state"] != "pending":
            return
        try:
            origin, token = self.config.connection(
                receipt["organization_id"], receipt["target_host"]
            )
        except LookupError as error:
            self._record_delivery_rejection(receipt["id"], error)
            return
        try:
            response = await self.transport(origin, token, receipt["envelope"])
            self._validate_acceptance(receipt, response)
        except httpx.HTTPStatusError as error:
            if _definitive_rejection(error):
                self._record_delivery_rejection(
                    receipt["id"],
                    RuntimeError(
                        "Peer receiver rejected delivery before acceptance "
                        f"(HTTP {error.response.status_code})"
                    ),
                )
            else:
                self._record_delivery_failure(receipt["id"], error)
            return
        except (
            httpx.HTTPError,
            LookupError,
            PermissionError,
            RuntimeUnavailable,
            ValueError,
        ) as error:
            self._record_delivery_failure(receipt["id"], error)
            return
        self._accept(receipt["id"], response)

    async def _deliver_with_timeout(self, receipt: dict[str, Any]) -> None:
        lock = self.delivery_locks.setdefault(receipt["id"], asyncio.Lock())
        async with lock:
            current = self._pending_delivery(receipt["id"])
            if current is None or current["next_attempt"] > time.time():
                return
            admitted = self._admit_delivery_attempt(receipt["id"])
            if admitted is None:
                return
            try:
                if admitted["target_host"] == str(self.store.instance_id):
                    envelope = PeerEnvelope.model_validate(admitted["envelope"])
                    try:
                        self._validate_local_target(envelope)
                    except (LookupError, PermissionError) as error:
                        self._record_delivery_rejection(admitted["id"], error)
                        return
                    accepted = await asyncio.wait_for(
                        self.receive(str(self.store.instance_id), envelope), timeout=10
                    )
                    self._validate_acceptance(admitted, accepted)
                    self._accept(admitted["id"], accepted)
                else:
                    await asyncio.wait_for(self._deliver(admitted), timeout=10)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - persisted by the supervisor.
                self._record_delivery_failure(admitted["id"], error)

    @staticmethod
    def _validate_acceptance(receipt: dict[str, Any], response: Any) -> None:
        if (
            not isinstance(response, dict)
            or response.get("state") != "accepted"
            or response.get("organization_id") != receipt["organization_id"]
            or response.get("source_host") != receipt["envelope"]["source_host"]
            or response.get("source_agent") != receipt["envelope"]["source_agent"]
            or response.get("source_session") != receipt["envelope"]["source_session"]
            or response.get("id") != receipt["id"]
            or response.get("target_host") != receipt["target_host"]
            or response.get("target_agent") != receipt["envelope"]["target_agent"]
            or not isinstance(response.get("target_session"), str)
            or not _native_id(response["target_session"])
            or response.get("dispatch_id") != receipt["id"]
            or (
                receipt["envelope"]["target_session"] is not None
                and response["target_session"] != receipt["envelope"]["target_session"]
            )
        ):
            raise RuntimeUnavailable("Peer acceptance receipt is invalid")

    def _record_delivery_failure(self, delivery_id: str, error: Exception) -> None:
        with self.store.connect() as connection:
            attempts = connection.execute(
                "SELECT attempts FROM peer_outbox WHERE id=? AND state='pending'", (delivery_id,)
            ).fetchone()
            if attempts is None:
                return
            connection.execute(
                "UPDATE peer_outbox SET attempts=?,next_attempt=?,error=?,updated_at=unixepoch() "
                "WHERE id=? AND state='pending'",
                (
                    attempts["attempts"],
                    time.time() + min(60, 2 ** min(attempts["attempts"], 6)),
                    str(error),
                    delivery_id,
                ),
            )

    def _record_delivery_rejection(self, delivery_id: str, error: Exception) -> None:
        """Retain a verified pre-acceptance refusal without retrying the envelope."""

        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT attempts FROM peer_outbox WHERE id=? AND state='pending'", (delivery_id,)
            ).fetchone()
            if row is None:
                return
            if row["attempts"] > 1:
                connection.execute(
                    "UPDATE peer_outbox SET state='uncertain',next_attempt=0,error=?,"
                    "updated_at=unixepoch() WHERE id=? AND state='pending'",
                    (
                        f"Peer delivery outcome is uncertain after a prior unverified attempt: {error}",
                        delivery_id,
                    ),
                )
            else:
                connection.execute(
                    "UPDATE peer_outbox SET state='rejected',next_attempt=0,error=?,"
                    "updated_at=unixepoch() WHERE id=? AND state='pending'",
                    (str(error), delivery_id),
                )
        self._publish_terminal_notices()

    def _pending_delivery(self, delivery_id: str) -> dict[str, Any] | None:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM peer_outbox WHERE id=? AND state='pending'", (delivery_id,)
            ).fetchone()
        return _outbox(row) if row is not None else None

    def _admit_delivery_attempt(self, delivery_id: str) -> dict[str, Any] | None:
        """Persist an outbound attempt before any local native or remote transport await."""

        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM peer_outbox WHERE id=? AND state='pending'", (delivery_id,)
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE peer_outbox SET attempts=attempts+1,updated_at=unixepoch() "
                "WHERE id=? AND state='pending'",
                (delivery_id,),
            )
            admitted = connection.execute(
                "SELECT * FROM peer_outbox WHERE id=?", (delivery_id,)
            ).fetchone()
        return _outbox(admitted) if admitted is not None else None

    def _validate_local_target(self, envelope: PeerEnvelope) -> None:
        """Validate the same-host target before a receive can reserve or invoke native work."""

        target = self.config.agent(str(envelope.organization_id), str(envelope.target_agent))
        if target["host_id"] != str(self.store.instance_id):
            raise PermissionError("Peer target agent is not hosted here")
        if envelope.target_session is not None:
            self.store.session(
                str(envelope.organization_id), str(envelope.target_agent), envelope.target_session
            )

    def _publish_results(self) -> None:
        """Return each terminal peer outcome once, as a distinct peer message.

        Result envelopes are deliberately not inspected here.  They are terminal
        notifications to the original source thread, rather than another work
        request which could create a notification loop.
        """
        with self.store.connect() as connection:
            inboxes = connection.execute(
                "SELECT * FROM peer_inbox WHERE state='accepted'"
            ).fetchall()
        for row in inboxes:
            inbox = dict(row)
            envelope = PeerEnvelope.model_validate_json(inbox["envelope"])
            if envelope.kind == "result" or not inbox["dispatch_id"] or inbox["result_id"]:
                continue
            delivery = self.dispatch.get(
                inbox["organization_id"], inbox["target_agent"], inbox["dispatch_id"]
            )
            if delivery["state"] not in {"completed", "failed", "contributed", "cancelled"}:
                continue
            if inbox["id"] in self.result_tasks:
                continue
            self.result_tasks[inbox["id"]] = asyncio.create_task(
                self._publish_result(inbox, envelope, delivery)
            )

    def _publish_terminal_notices(self) -> None:
        """Queue one local, attributable result for each terminal work outcome."""

        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM peer_outbox WHERE state IN ('rejected','uncertain') AND result_id IS NULL"
            ).fetchall()
        for row in rows:
            receipt = _outbox(row)
            envelope = PeerEnvelope.model_validate(receipt["envelope"])
            result_id = uuid5(NAMESPACE_URL, f"fesnyng-peer-terminal:{receipt['id']}")
            if envelope.kind == "result":
                self._mark_terminal_notice_published(receipt["id"], result_id)
                continue
            try:
                target = self.config.agent(receipt["organization_id"], str(envelope.target_agent))
                author = Actor(
                    kind="agent",
                    id=envelope.target_agent,
                    name=target["name"],
                )
            except LookupError:
                # A stale roster must not turn a terminal delivery rejection
                # back into a retryable work envelope. The durable row remains
                # eligible for its local result notice when configuration returns.
                continue
            try:
                self.dispatch.enqueue(
                    receipt["organization_id"],
                    receipt["source_agent"],
                    receipt["source_session"],
                    Submission(
                        id=result_id,
                        text=_terminal_notice_text(receipt),
                        mode="queued",
                        origin_id=UUID(receipt["id"]),
                    ),
                    author,
                )
            except (LookupError, ValueError):
                continue
            self._mark_terminal_notice_published(receipt["id"], result_id)
            self.dispatcher.wake()

    def _mark_terminal_notice_published(self, delivery_id: str, result_id: UUID) -> None:
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE peer_outbox SET result_id=?,updated_at=unixepoch() "
                "WHERE id=? AND state IN ('rejected','uncertain') AND result_id IS NULL",
                (str(result_id), delivery_id),
            )

    async def _publish_result(
        self, inbox: dict[str, Any], envelope: PeerEnvelope, delivery: dict[str, Any]
    ) -> None:
        try:
            await asyncio.wait_for(self._publish_result_once(inbox, envelope, delivery), timeout=10)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - preserve uncertain return without replaying effects.
            self._record_result_uncertainty(inbox["id"], error)

    async def _publish_result_once(
        self, inbox: dict[str, Any], envelope: PeerEnvelope, delivery: dict[str, Any]
    ) -> None:
        result_id = uuid5(NAMESPACE_URL, f"fesnyng-peer-result:{inbox['id']}")
        result = PeerEnvelope(
            id=result_id,
            organization_id=inbox["organization_id"],
            source_host=self.store.instance_id,
            source_agent=inbox["target_agent"],
            source_session=inbox["target_session"],
            target_agent=inbox["source_agent"],
            target_session=inbox["source_session"],
            text=await self._result_text(inbox, delivery),
            mode="queued",
            workspace=envelope.workspace,
            title=envelope.title,
            origin_id=UUID(inbox["id"]),
            kind="result",
        )
        encoded = result.model_dump_json()
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM peer_outbox WHERE id=?", (str(result_id),)
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO peer_outbox(id,organization_id,source_agent,source_session,"
                    "target_host,envelope) VALUES(?,?,?,?,?,?)",
                    (
                        str(result_id),
                        inbox["organization_id"],
                        inbox["target_agent"],
                        inbox["target_session"],
                        inbox["source_host"],
                        encoded,
                    ),
                )
                connection.execute(
                    "UPDATE peer_inbox SET result_id=?,error=NULL,updated_at=unixepoch() WHERE id=?",
                    (str(result_id), inbox["id"]),
                )

    async def _result_text(self, inbox: dict[str, Any], delivery: dict[str, Any]) -> str:
        outcome = delivery["outcome"] if isinstance(delivery["outcome"], dict) else {}
        message_id = outcome.get("message_id")
        if isinstance(message_id, str):
            session = self.store.session(
                inbox["organization_id"], inbox["target_agent"], inbox["target_session"]
            )
            history = await self.runtime.request(
                inbox["organization_id"],
                inbox["target_agent"],
                f"/session/{inbox['target_session']}/message",
                directory=session["directory"],
            )
            found, answer = _assistant_text(
                history, inbox["target_session"], message_id, outcome.get("responding_to")
            )
            if answer:
                return answer
            if found:
                detail = delivery["error"] or outcome.get("kind", "terminal outcome")
                return (
                    f"Peer delivery {inbox['id']} is {delivery['state']} for agent "
                    f"{inbox['target_agent']} in thread {inbox['target_session']} "
                    f"at native response {message_id}: {detail}"
                )
            raise RuntimeUnavailable("Native peer outcome response is unavailable")
        detail = delivery["error"] or outcome.get("kind", "terminal outcome")
        return (
            f"Peer delivery {inbox['id']} is {delivery['state']} for agent "
            f"{inbox['target_agent']} in thread {inbox['target_session']}: {detail}"
        )

    def _record_result_uncertainty(self, inbox_id: str, error: Exception) -> None:
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE peer_inbox SET error=?,updated_at=unixepoch() WHERE id=? AND state='accepted'",
                (f"Peer outcome remains uncertain: {error}", inbox_id),
            )

    def _accept(self, delivery_id: str, receipt: dict[str, Any]) -> None:
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE peer_outbox SET state='accepted',receipt=?,error=NULL,updated_at=unixepoch() WHERE id=?",
                (json.dumps(receipt), delivery_id),
            )

    async def _reserve_session(self, inbox: dict[str, Any], envelope: PeerEnvelope) -> str:
        try:
            session = await self.runtime.create_session(
                inbox["organization_id"],
                inbox["target_agent"],
                envelope.title,
                envelope.workspace,
                directory=inbox["directory"],
                metadata={"fesnyng_delivery_id": inbox["id"]},
            )
        except RuntimeUnavailable:
            adopted = await self._adopt_session(inbox)
            if adopted is None:
                with self.store.connect() as connection:
                    connection.execute(
                        "UPDATE peer_inbox SET state='uncertain',error='Native session creation outcome unknown',updated_at=unixepoch() WHERE id=?",
                        (inbox["id"],),
                    )
                raise
            session = adopted
        return self._save_or_validate_session(inbox, session, envelope.title)

    def _save_or_validate_session(self, inbox: dict[str, Any], session: Any, title: str) -> str:
        session_id = session.get("id") if isinstance(session, dict) else None
        if not isinstance(session_id, str) or not _native_id(session_id):
            raise RuntimeUnavailable("Native peer session receipt is invalid")
        try:
            existing = self.store.session(
                inbox["organization_id"], inbox["target_agent"], session_id
            )
        except LookupError:
            try:
                self.store.save_session(
                    inbox["organization_id"],
                    inbox["target_agent"],
                    session_id,
                    inbox["directory"],
                    title,
                )
            except sqlite3.IntegrityError as error:
                raise RuntimeUnavailable("Native peer session id is already owned") from error
        else:
            if existing["directory"] != inbox["directory"] or existing["title"] != title:
                raise RuntimeUnavailable("Native peer session does not match its reservation")
        return session_id

    def _local_session_match(self, inbox: dict[str, Any]) -> dict[str, Any] | None:
        matches = [
            session
            for session in self.store.sessions(inbox["organization_id"], inbox["target_agent"])
            if session["directory"] == inbox["directory"]
        ]
        if len(matches) != 1:
            return None
        return {"id": matches[0]["session_id"]}

    async def _adopt_session(self, inbox: dict[str, Any]) -> dict[str, Any] | None:
        sessions = await self.runtime.request(
            inbox["organization_id"],
            inbox["target_agent"],
            "/session",
            directory=inbox["directory"],
        )
        if not isinstance(sessions, list):
            raise RuntimeUnavailable("Native session lookup is invalid")
        matches = [
            session
            for session in sessions
            if isinstance(session, dict)
            and isinstance(session.get("metadata"), dict)
            and session["metadata"].get("fesnyng_delivery_id") == inbox["id"]
            and session.get("directory") == inbox["directory"]
            and session.get("parentID") is None
            and isinstance(session.get("id"), str)
            and _native_id(session["id"])
        ]
        if len(matches) != 1:
            return None
        return matches[0]

    def _inbox(self, delivery_id: str) -> dict[str, Any]:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM peer_inbox WHERE id=?", (delivery_id,)
            ).fetchone()
        if row is None:
            raise LookupError("Peer reservation not found")
        return dict(row)

    def _inbox_receipt(self, row: Any) -> dict[str, Any]:
        value = dict(row)
        return {
            "id": value["id"],
            "organization_id": value["organization_id"],
            "source_host": value["source_host"],
            "source_agent": value["source_agent"],
            "source_session": value["source_session"],
            "target_host": str(self.store.instance_id),
            "target_agent": value["target_agent"],
            "target_session": value["target_session"],
            "dispatch_id": value["dispatch_id"],
            "state": value["state"],
        }


async def _post_envelope(origin: str, token: str, envelope: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(base_url=origin, timeout=10) as client:
        response = await client.post(
            f"/organizations/{envelope['organization_id']}/peers/deliveries",
            headers={"Authorization": f"Bearer {token}"},
            json=envelope,
        )
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict):
        raise RuntimeUnavailable("Peer acceptance response is invalid")
    return value


def _definitive_rejection(error: httpx.HTTPStatusError) -> bool:
    """Only retryable/ambiguous client statuses keep an unverified envelope pending."""

    status = error.response.status_code
    return 400 <= status < 500 and status not in {408, 409, 425, 429}


def _terminal_notice_text(receipt: dict[str, Any]) -> str:
    if receipt["state"] == "rejected":
        return (
            f"Peer delivery {receipt['id']} was rejected before target acceptance: "
            f"{receipt['error']}"
        )
    return (
        f"Peer delivery {receipt['id']} has an uncertain outcome: prior acceptance is unknown, "
        "automatic retries stopped; inspect receiving history/receipt before new work. "
        f"Last observed error: {receipt['error']}"
    )


def _outbox(row: Any) -> dict[str, Any]:
    value = dict(row)
    value["envelope"] = json.loads(value["envelope"])
    value["receipt"] = json.loads(value["receipt"]) if value["receipt"] else None
    return value


def _directory(envelope: PeerEnvelope) -> str:
    return f"/workspace/{envelope.workspace}/threads/peer-{envelope.id.hex}"


def _assistant_text(
    history: Any, session_id: str, message_id: str, responding_to: Any
) -> tuple[bool, str | None]:
    if not isinstance(history, list):
        raise RuntimeUnavailable("Native peer outcome history is invalid")
    for message in history:
        if not isinstance(message, dict):
            raise RuntimeUnavailable("Native peer outcome history is invalid")
        info, parts = message.get("info"), message.get("parts")
        if not isinstance(info, dict) or not isinstance(parts, list):
            raise RuntimeUnavailable("Native peer outcome history is invalid")
        if (
            info.get("id") != message_id
            or info.get("sessionID") != session_id
            or info.get("role") != "assistant"
            or (isinstance(responding_to, str) and info.get("parentID") != responding_to)
        ):
            continue
        text = "\n".join(
            part["text"]
            for part in parts
            if isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        ).strip()
        return True, text[:16_000] or None
    return False, None


def _native_id(value: str) -> bool:
    return re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value) is not None
