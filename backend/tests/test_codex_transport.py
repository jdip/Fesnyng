import asyncio
import json
from collections.abc import AsyncGenerator

import pytest

from fesnyng_backend.codex_transport import CodexTransport
from fesnyng_backend.host_runtime import RuntimeUnavailable


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.received: asyncio.Queue[str] = asyncio.Queue()
        self.closed = False

    async def send(self, message: str) -> None:
        payload = json.loads(message)
        self.sent.append(payload)
        if payload.get("method") in {"initialize", "account/login/start", "account/logout"}:
            await self.received.put(json.dumps({"id": payload["id"], "result": {}}))
        elif payload.get("method") == "thread/start":
            await self.received.put(
                json.dumps({"id": payload["id"], "result": {"thread": {"id": "thr_1"}}})
            )

    async def recv(self) -> str:
        return await self.received.get()

    async def close(self) -> None:
        self.closed = True


class BrokenSendSocket(FakeSocket):
    async def send(self, message: str) -> None:
        del message
        raise OSError("socket closed")


async def _events(transport: CodexTransport) -> AsyncGenerator[dict[str, object]]:
    async for event in transport.events("org", "agent"):
        yield event


async def _credential(_token: str, **_kwargs: object) -> dict[str, object]:
    return {"access": "access-token", "account_id": "account", "generation": 1}


def test_transport_initializes_logs_in_and_relays_native_events():
    socket = FakeSocket()

    async def connect(uri: str, token: str):
        assert uri == "ws://127.0.0.1:4096"
        assert token == "runtime-secret"
        return socket

    async def credential(*args, **kwargs):
        assert args == ("agent-token",)
        return {"access": "access-token", "account_id": "account", "generation": 4}

    transport = CodexTransport(
        connect=connect,
        endpoint=lambda _org, _agent: ("ws://127.0.0.1:4096", "runtime-secret", "agent-token"),
        credential_access=credential,
    )

    async def check():
        result = await transport.call("org", "agent", "thread/start", {"cwd": "/workspace"})
        assert result == {"thread": {"id": "thr_1"}}
        await socket.received.put(
            json.dumps({"method": "turn/started", "params": {"turn": {"id": "turn_1"}}})
        )
        events = _events(transport)
        assert await anext(events) == {
            "method": "turn/started",
            "params": {"turn": {"id": "turn_1"}},
        }
        await events.aclose()
        await transport.close()

    asyncio.run(check())
    initialize = socket.sent[0]
    assert initialize["method"] == "initialize"
    assert isinstance(initialize["params"], dict)
    assert initialize["params"]["capabilities"] == {"experimentalApi": True}
    assert socket.sent[1] == {"method": "initialized", "params": {}}
    assert socket.sent[2]["method"] == "account/login/start"
    assert socket.sent[2]["params"] == {
        "type": "chatgptAuthTokens",
        "accessToken": "access-token",
        "chatgptAccountId": "account",
    }
    assert socket.closed


def test_transport_refreshes_only_the_owning_connection_and_rejects_foreign_responses():
    socket = FakeSocket()
    refreshes: list[tuple[object, object, object]] = []

    async def connect(_uri: str, _token: str):
        return socket

    async def credential(token, *, rejected_generation=None, previous_account_id=None):
        refreshes.append((token, rejected_generation, previous_account_id))
        if rejected_generation is None:
            return {"access": "original", "account_id": "account", "generation": 4}
        return {"access": "fresh", "account_id": "account", "generation": 5}

    transport = CodexTransport(
        connect=connect,
        endpoint=lambda _org, _agent: ("ws://native", "secret", "agent-token"),
        credential_access=credential,
    )

    async def check():
        await transport.call("org", "agent", "thread/start", {})
        await socket.received.put(
            json.dumps(
                {
                    "id": "refresh-1",
                    "method": "account/chatgptAuthTokens/refresh",
                    "params": {"previousAccountId": "account"},
                }
            )
        )
        for _ in range(20):
            if any(message.get("id") == "refresh-1" for message in socket.sent):
                break
            await asyncio.sleep(0)
        assert refreshes[-1] == ("agent-token", 4, "account")
        assert socket.sent[-1] == {
            "id": "refresh-1",
            "result": {"accessToken": "fresh", "chatgptAccountId": "account"},
        }
        await socket.received.put(
            json.dumps(
                {
                    "id": "approval-1",
                    "method": "item/commandExecution/requestApproval",
                    "params": {"threadId": "thr_1", "turnId": "turn_1"},
                }
            )
        )
        for _ in range(20):
            pending = await transport.pending("org", "agent", "thr_1")
            if pending:
                break
            await asyncio.sleep(0)
        assert len(pending) == 1
        assert pending[0] == {
            "id": pending[0]["id"],
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "thr_1", "turnId": "turn_1"},
            "thread_id": "thr_1",
            "turn_id": "turn_1",
        }
        assert pending[0]["id"] != "approval-1"
        with pytest.raises(RuntimeUnavailable, match="stale"):
            await transport.respond(
                "other-org", "agent", "thr_1", pending[0]["id"], {"decision": "accept"}
            )
        assert (
            await transport.respond(
                "org", "agent", "thr_1", pending[0]["id"], {"decision": "accept"}
            )
            == {}
        )
        assert socket.sent[-1] == {"id": "approval-1", "result": {"decision": "accept"}}
        await transport.close()

    asyncio.run(check())


def test_slow_event_subscriber_is_woken_to_reconnect():
    socket = FakeSocket()

    async def connect(_uri: str, _token: str):
        return socket

    transport = CodexTransport(
        connect=connect,
        endpoint=lambda _org, _agent: ("ws://native", "secret", "agent-token"),
        credential_access=_credential,
    )

    async def check():
        await transport.call("org", "agent", "thread/start", {})
        events = _events(transport)
        first = asyncio.ensure_future(anext(events))
        await asyncio.sleep(0)
        await socket.received.put(json.dumps({"method": "turn/started", "params": {}}))
        assert await first == {"method": "turn/started", "params": {}}

        for _ in range(257):
            await socket.received.put(json.dumps({"method": "item/updated", "params": {}}))
        await asyncio.sleep(0)
        with pytest.raises(RuntimeUnavailable, match="connection unavailable"):
            await anext(events)
        await events.aclose()
        await transport.close()

    asyncio.run(check())


def test_send_failure_discards_the_unpublished_connection():
    socket = BrokenSendSocket()

    async def connect(_uri: str, _token: str):
        return socket

    transport = CodexTransport(
        connect=connect,
        endpoint=lambda _org, _agent: ("ws://native", "secret", "agent-token"),
        credential_access=_credential,
    )

    async def check():
        with pytest.raises(RuntimeUnavailable, match="connection unavailable"):
            await transport.call("org", "agent", "thread/start", {})
        assert transport.connections == {}
        assert socket.closed

    asyncio.run(check())


def test_cancelled_call_response_does_not_stop_the_reader():
    socket = FakeSocket()

    async def connect(_uri: str, _token: str):
        return socket

    transport = CodexTransport(
        connect=connect,
        endpoint=lambda _org, _agent: ("ws://native", "secret", "agent-token"),
        credential_access=_credential,
    )

    async def check():
        await transport.call("org", "agent", "thread/start", {})
        connection = transport.connections[("org", "agent")]
        stale = asyncio.get_running_loop().create_future()
        stale.cancel()
        connection.pending_calls[91] = stale
        await socket.received.put(json.dumps({"id": 91, "result": {}}))
        await asyncio.sleep(0)
        assert connection.reader is not None and not connection.reader.done()
        await transport.close()

    asyncio.run(check())


def test_missing_credential_logs_out_before_credential_free_management():
    socket = FakeSocket()

    async def connect(_uri: str, _token: str):
        return socket

    async def revoked(_token: str):
        raise PermissionError("assignment revoked")

    transport = CodexTransport(
        connect=connect,
        endpoint=lambda _org, _agent: ("ws://native", "secret", "agent-token"),
        credential_access=revoked,
    )

    async def check():
        assert await transport.call("org", "agent", "thread/start", {}) == {
            "thread": {"id": "thr_1"}
        }
        assert any(item.get("method") == "account/logout" for item in socket.sent)
        assert ("org", "agent") in transport.connections
        await transport.close()
        assert socket.closed

    asyncio.run(check())


def test_rejected_logout_cannot_publish_a_connection_with_a_stale_account():
    class LogoutRejected(FakeSocket):
        async def send(self, message):
            payload = json.loads(message)
            if payload.get("method") == "account/logout":
                self.sent.append(payload)
                await self.received.put(
                    json.dumps(
                        {
                            "id": payload["id"],
                            "error": {"code": -32000, "message": "Logout failed"},
                        }
                    )
                )
            else:
                await super().send(message)

    socket = LogoutRejected()

    async def connect(_uri, _token):
        return socket

    transport = CodexTransport(
        connect=connect,
        endpoint=lambda _org, _agent: ("ws://native", "secret", "agent-token"),
    )

    async def check():
        with pytest.raises(RuntimeUnavailable, match="Logout failed"):
            await transport.call("org", "agent", "thread/start", {})
        assert not transport.connections
        assert socket.closed
        assert not any(item.get("method") == "thread/start" for item in socket.sent)

    asyncio.run(check())


def test_mcp_elicitation_without_a_turn_is_scoped_and_unknown_requests_are_rejected():
    socket = FakeSocket()

    async def connect(_uri: str, _token: str):
        return socket

    transport = CodexTransport(
        connect=connect,
        endpoint=lambda _org, _agent: ("ws://native", "secret", "agent-token"),
        credential_access=_credential,
    )

    async def check():
        await transport.call("org", "agent", "thread/start", {})
        await socket.received.put(
            json.dumps(
                {
                    "id": 1,
                    "method": "mcpServer/elicitation/request",
                    "params": {"threadId": "thr_1", "serverName": "fesnyng"},
                }
            )
        )
        for _ in range(20):
            pending = await transport.pending("org", "agent", "thr_1")
            if pending:
                break
            await asyncio.sleep(0)
        assert pending[0]["turn_id"] is None
        await socket.received.put(
            json.dumps({"id": "unsupported", "method": "attestation/generate", "params": {}})
        )
        for _ in range(20):
            if any(item.get("id") == "unsupported" for item in socket.sent):
                break
            await asyncio.sleep(0)
        assert socket.sent[-1] == {
            "id": "unsupported",
            "error": {"code": -32601, "message": "Unsupported server request"},
        }
        await transport.close()

    asyncio.run(check())


def test_refresh_timeout_returns_a_native_error_without_stalling_the_reader(monkeypatch):
    socket = FakeSocket()
    monkeypatch.setattr("fesnyng_backend.codex_transport._CREDENTIAL_TIMEOUT", 0.01)

    async def connect(_uri: str, _token: str):
        return socket

    async def credential(_token: str, *, rejected_generation=None, previous_account_id=None):
        del previous_account_id
        if rejected_generation is None:
            return {"access": "original", "account_id": "account", "generation": 1}
        await asyncio.sleep(1)
        return {"access": "late", "account_id": "account", "generation": 2}

    transport = CodexTransport(
        connect=connect,
        endpoint=lambda _org, _agent: ("ws://native", "secret", "agent-token"),
        credential_access=credential,
    )

    async def check():
        await transport.call("org", "agent", "thread/start", {})
        await socket.received.put(
            json.dumps(
                {
                    "id": "refresh-timeout",
                    "method": "account/chatgptAuthTokens/refresh",
                    "params": {"previousAccountId": "account"},
                }
            )
        )
        await asyncio.sleep(0.02)
        assert socket.sent[-1] == {
            "id": "refresh-timeout",
            "error": {"code": -32000, "message": "Credential unavailable"},
        }
        assert await transport.call("org", "agent", "thread/start", {}) == {
            "thread": {"id": "thr_1"}
        }
        await transport.close()

    asyncio.run(check())
