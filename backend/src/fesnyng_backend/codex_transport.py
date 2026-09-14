"""Authenticated, bounded JSON-RPC transport for one Codex App Server connection."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, cast
from uuid import uuid4

from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import ConnectionClosed

from fesnyng_backend.host_runtime import RuntimeUnavailable


class Socket(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


CredentialAccess = Callable[..., Awaitable[dict[str, object]]]
Endpoint = Callable[[str, str], tuple[str, str, str]]
Connector = Callable[[str, str], Awaitable[Socket]]
_CREDENTIAL_TIMEOUT = 10
_TURN_SCOPED_REQUESTS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/tool/requestUserInput",
    "item/permissions/requestApproval",
}
_MCP_ELICITATION_REQUEST = "mcpServer/elicitation/request"


async def _connect(uri: str, token: str) -> Socket:
    try:
        return cast(
            Socket,
            await websocket_connect(
                uri,
                additional_headers={"Authorization": f"Bearer {token}"},
                open_timeout=10,
                close_timeout=5,
                max_size=2 * 1024 * 1024,
            ),
        )
    except Exception as error:
        raise RuntimeUnavailable("Codex App Server connection unavailable") from error


@dataclass
class _Connection:
    socket: Socket
    organization_id: str
    agent_id: str
    agent_token: str
    generation: str = field(default_factory=lambda: str(uuid4()))
    pending_calls: dict[int, asyncio.Future[dict[str, Any]]] = field(default_factory=dict)
    pending_requests: dict[str, dict[str, Any]] = field(default_factory=dict)
    subscribers: set[asyncio.Queue[dict[str, Any] | None]] = field(default_factory=set)
    next_id: int = 1
    credential_generation: int | None = None
    account_id: str | None = None
    reader: asyncio.Task[None] | None = None


class CodexTransport:
    """Own persistent per-agent App Server connections without exposing raw sockets."""

    def __init__(
        self,
        *,
        endpoint: Endpoint,
        credential_access: CredentialAccess | None = None,
        connect: Connector = _connect,
    ) -> None:
        self.endpoint = endpoint
        self.credential_access = credential_access
        self.connect = connect
        self.connections: dict[tuple[str, str], _Connection] = {}
        self.locks: dict[tuple[str, str], asyncio.Lock] = {}

    def set_credential_access(self, credential_access: CredentialAccess) -> None:
        self.credential_access = credential_access

    async def call(
        self, organization_id: str, agent_id: str, method: str, params: Mapping[str, object]
    ) -> dict[str, Any]:
        connection = await self._connection(organization_id, agent_id)
        return await self._request(connection, method, dict(params))

    async def pending(
        self, organization_id: str, agent_id: str, thread_id: str
    ) -> list[dict[str, Any]]:
        connection = await self._connection(organization_id, agent_id)
        return [
            {
                "id": request["id"],
                "method": request["method"],
                "params": request["params"],
                "thread_id": request["thread_id"],
                "turn_id": request["turn_id"],
            }
            for request in connection.pending_requests.values()
            if request["thread_id"] == thread_id
        ]

    async def connection_id(self, organization_id: str, agent_id: str) -> str:
        """Return an opaque connection generation after its handshake completes."""
        return (await self._connection(organization_id, agent_id)).generation

    async def respond(
        self,
        organization_id: str,
        agent_id: str,
        thread_id: str,
        request_id: str,
        response: Mapping[str, object],
    ) -> dict[str, Any]:
        connection = await self._connection(organization_id, agent_id)
        request = connection.pending_requests.get(request_id)
        if request is None or request["connection"] is not connection:
            raise RuntimeUnavailable("Codex server request is stale")
        if request["thread_id"] != thread_id:
            raise RuntimeUnavailable("Codex server request does not belong to this thread")
        await self._send(connection, {"id": request["wire_id"], "result": dict(response)})
        del connection.pending_requests[request_id]
        return {}

    async def events(self, organization_id: str, agent_id: str) -> AsyncIterator[dict[str, Any]]:
        connection = await self._connection(organization_id, agent_id)
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=256)
        connection.subscribers.add(queue)
        try:
            while self.connections.get((organization_id, agent_id)) is connection:
                event = await queue.get()
                if event is None:
                    raise RuntimeUnavailable("Codex App Server connection unavailable")
                yield event
        finally:
            connection.subscribers.discard(queue)

    async def close(self) -> None:
        for connection in list(self.connections.values()):
            await self._discard(connection)

    async def close_agent(self, organization_id: str, agent_id: str) -> None:
        """Forget a connection before its credential assignment is replaced."""
        connection = self.connections.get((organization_id, agent_id))
        if connection is not None:
            await self._discard(connection)

    async def _connection(self, organization_id: str, agent_id: str) -> _Connection:
        key = organization_id, agent_id
        existing = self.connections.get(key)
        if existing is not None:
            return existing
        lock = self.locks.setdefault(key, asyncio.Lock())
        async with lock:
            existing = self.connections.get(key)
            if existing is not None:
                return existing
            uri, token, agent_token = self.endpoint(organization_id, agent_id)
            socket = await self.connect(uri, token)
            connection = _Connection(socket, organization_id, agent_id, agent_token)
            connection.reader = asyncio.create_task(self._read(connection))
            try:
                await self._request(
                    connection,
                    "initialize",
                    {
                        "clientInfo": {
                            "name": "fesnyng",
                            "title": "Fesnyng Agent Host",
                            "version": "0.1.0",
                        },
                        "capabilities": {"experimentalApi": True},
                    },
                )
                await self._send(connection, {"method": "initialized", "params": {}})
                await self._login(connection)
                self.connections[key] = connection
            except BaseException:
                await self._discard(connection)
                raise
            return connection

    async def _login(self, connection: _Connection) -> None:
        if self.credential_access is None:
            await self._logout(connection)
            return
        try:
            credential = await self.credential_access(connection.agent_token)
            access, account_id, generation = _credential(credential)
        except PermissionError:
            await self._logout(connection)
            return
        except (RuntimeUnavailable, ValueError) as error:
            await self._logout(connection)
            raise RuntimeUnavailable("Codex credential unavailable") from error
        await self._request(
            connection,
            "account/login/start",
            {
                "type": "chatgptAuthTokens",
                "accessToken": access,
                "chatgptAccountId": account_id,
            },
        )
        connection.account_id = account_id
        connection.credential_generation = generation

    async def _request(
        self, connection: _Connection, method: str, params: dict[str, Any] | None
    ) -> dict[str, Any]:
        request_id = connection.next_id
        connection.next_id += 1
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        connection.pending_calls[request_id] = future
        try:
            payload: dict[str, object] = {"method": method, "id": request_id}
            if params is not None:
                payload["params"] = params
            await self._send(connection, payload)
            return await asyncio.wait_for(future, timeout=30)
        except TimeoutError:
            raise RuntimeUnavailable("Codex App Server request timed out") from None
        finally:
            connection.pending_calls.pop(request_id, None)

    async def _send(self, connection: _Connection, payload: Mapping[str, object]) -> None:
        try:
            await connection.socket.send(json.dumps(payload, separators=(",", ":")))
        except Exception as error:
            await self._discard(connection)
            raise RuntimeUnavailable("Codex App Server connection unavailable") from error

    async def _read(self, connection: _Connection) -> None:
        try:
            while True:
                raw = await connection.socket.recv()
                if not isinstance(raw, str):
                    raise RuntimeUnavailable("Codex App Server returned an invalid response")
                message = json.loads(raw)
                if not isinstance(message, dict):
                    continue
                request_id = message.get("id")
                if request_id is not None and isinstance(message.get("method"), str):
                    await self._server_request(connection, request_id, message)
                elif isinstance(request_id, int) and request_id in connection.pending_calls:
                    future = connection.pending_calls[request_id]
                    if future.done():
                        continue
                    if "error" in message:
                        error = message["error"]
                        detail = error.get("message") if isinstance(error, dict) else None
                        future.set_exception(
                            RuntimeUnavailable(detail or "Codex App Server rejected request")
                        )
                    elif isinstance(message.get("result"), dict):
                        future.set_result(message["result"])
                    else:
                        future.set_exception(
                            RuntimeUnavailable("Codex App Server returned an invalid response")
                        )
                elif isinstance(message.get("method"), str):
                    for queue in tuple(connection.subscribers):
                        try:
                            queue.put_nowait(message)
                        except asyncio.QueueFull:
                            connection.subscribers.discard(queue)
                            self._wake_subscriber(queue)
        except asyncio.CancelledError:
            raise
        except (ConnectionClosed, OSError, RuntimeUnavailable, ValueError):
            await self._discard(connection)

    async def _server_request(
        self, connection: _Connection, request_id: object, message: dict[str, Any]
    ) -> None:
        method, params = message["method"], message.get("params")
        if method == "account/chatgptAuthTokens/refresh":
            await self._refresh(connection, request_id, params)
            return
        if method not in _TURN_SCOPED_REQUESTS | {_MCP_ELICITATION_REQUEST}:
            await self._send(
                connection,
                {
                    "id": request_id,
                    "error": {"code": -32601, "message": "Unsupported server request"},
                },
            )
            return
        if not isinstance(params, dict):
            await self._send(
                connection,
                {"id": request_id, "error": {"code": -32602, "message": "Invalid request"}},
            )
            return
        thread_id, turn_id = params.get("threadId"), params.get("turnId")
        valid_turn = isinstance(turn_id, str) or (
            method == _MCP_ELICITATION_REQUEST and turn_id is None
        )
        if not isinstance(thread_id, str) or not valid_turn:
            await self._send(
                connection,
                {
                    "id": request_id,
                    "error": {"code": -32602, "message": "Thread request is invalid"},
                },
            )
            return
        opaque_id = str(uuid4())
        connection.pending_requests[opaque_id] = {
            "id": opaque_id,
            "wire_id": request_id,
            "method": method,
            "params": params,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "connection": connection,
        }

    async def _refresh(self, connection: _Connection, request_id: object, params: object) -> None:
        if self.credential_access is None or not isinstance(params, dict):
            await self._send(
                connection,
                {"id": request_id, "error": {"code": -32000, "message": "Credential unavailable"}},
            )
            return
        try:
            credential = await asyncio.wait_for(
                self.credential_access(
                    connection.agent_token,
                    rejected_generation=connection.credential_generation,
                    previous_account_id=params.get("previousAccountId"),
                ),
                timeout=_CREDENTIAL_TIMEOUT,
            )
            access, account_id, generation = _credential(credential)
        except (PermissionError, RuntimeUnavailable, TimeoutError, ValueError):
            await self._send(
                connection,
                {"id": request_id, "error": {"code": -32000, "message": "Credential unavailable"}},
            )
            return
        connection.account_id = account_id
        connection.credential_generation = generation
        await self._send(
            connection,
            {
                "id": request_id,
                "result": {"accessToken": access, "chatgptAccountId": account_id},
            },
        )

    async def _discard(self, connection: _Connection) -> None:
        key = connection.organization_id, connection.agent_id
        if self.connections.get(key) is connection:
            del self.connections[key]
        for future in connection.pending_calls.values():
            if not future.done():
                future.set_exception(RuntimeUnavailable("Codex App Server connection unavailable"))
        connection.pending_requests.clear()
        for queue in tuple(connection.subscribers):
            self._wake_subscriber(queue)
        connection.subscribers.clear()
        current = asyncio.current_task()
        if connection.reader is not None and connection.reader is not current:
            connection.reader.cancel()
            await asyncio.gather(connection.reader, return_exceptions=True)
        try:
            await connection.socket.close()
        except (ConnectionClosed, OSError):
            pass

    async def _logout(self, connection: _Connection) -> None:
        await self._request(connection, "account/logout", None)

    @staticmethod
    def _wake_subscriber(queue: asyncio.Queue[dict[str, Any] | None]) -> None:
        while True:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        queue.put_nowait(None)


def _credential(value: Mapping[str, object]) -> tuple[str, str, int]:
    access, account_id, generation = (
        value.get("access"),
        value.get("account_id"),
        value.get("generation"),
    )
    if (
        not isinstance(access, str)
        or not access
        or not isinstance(account_id, str)
        or not account_id
    ):
        raise ValueError("Credential unavailable")
    if not isinstance(generation, int) or generation < 0:
        raise ValueError("Credential unavailable")
    return access, account_id, generation
