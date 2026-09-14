import asyncio
import json
import secrets
import sqlite3
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from fesnyng_backend.agent_models import AgentConfiguration
from fesnyng_backend.host_dispatch import DispatchStore
from fesnyng_backend.host_models import Actor, HostAgentConfiguration
from fesnyng_backend.host_runtime import DockerRuntime, RuntimeUnavailable
from fesnyng_backend.host_store import HostStore
from fesnyng_backend.peer_configuration import (
    PeerAgent,
    PeerConfiguration,
    PeerConfigurationStore,
    PeerHost,
)
from fesnyng_backend.peer_delivery import PeerDeliveryService, PeerEnvelope, PeerSend
from fesnyng_backend.peer_delivery_routes import router
from fesnyng_backend.settings import ServiceSettings


def test_same_host_peer_delivery_uses_durable_receiver_reservation_and_dispatch(tmp_path):
    host = HostStore(
        ServiceSettings(
            service="agent-host",
            state_directory=tmp_path / "state",
            database_path=tmp_path / "state/host.sqlite3",
        )
    )
    host.initialize()
    organization_id = str(uuid4())
    host.bind_organization(organization_id, secrets.token_urlsafe(32))
    source_agent, target_agent = uuid4(), uuid4()
    for agent_id, name in ((source_agent, "Source"), (target_agent, "Target")):
        envelope = HostAgentConfiguration(
            host_id=host.instance_id,
            organization_id=organization_id,
            agent_id=agent_id,
            version=1,
            name=name,
        )
        host.stage_agent(envelope)
        host.mark_applied(envelope)
    host.save_session(
        organization_id, str(source_agent), "ses_source", "/workspace/default/source", "Source"
    )
    configuration = PeerConfigurationStore(host)
    configuration.initialize()
    configuration.apply(
        PeerConfiguration(
            organization_id=organization_id,
            host_id=host.instance_id,
            version=1,
            agents=[
                PeerAgent(agent_id=source_agent, name="Source", title="", host_id=host.instance_id),
                PeerAgent(agent_id=target_agent, name="Target", title="", host_id=host.instance_id),
            ],
        )
    )
    dispatch = DispatchStore(host)
    dispatch.initialize()
    runtime = Native()
    dispatcher = Waker()
    service = PeerDeliveryService(host, configuration, runtime, dispatch, dispatcher)
    service.initialize()
    request = PeerSend(id=uuid4(), target_agent=target_agent, text="Please review")

    receipt = asyncio.run(service.send(organization_id, str(source_agent), "ses_source", request))

    assert receipt["state"] == "accepted"
    assert runtime.created == [
        (
            str(target_agent),
            "/workspace/default/threads/peer-" + request.id.hex,
            {"fesnyng_delivery_id": str(request.id)},
        )
    ]
    assert (
        asyncio.run(service.send(organization_id, str(source_agent), "ses_source", request))
        == receipt
    )
    assert dispatcher.woken
    deliveries = dispatch.for_thread(organization_id, str(target_agent), "ses_peer")
    assert deliveries[0]["author"]["kind"] == "agent"
    assert deliveries[0]["author"]["id"] == str(source_agent)
    with pytest.raises(ValueError, match="conflict"):
        asyncio.run(
            service.send(
                organization_id,
                str(source_agent),
                "ses_source",
                request.model_copy(update={"text": "Different"}),
            )
        )


def test_accepted_peer_retry_remains_readable_after_receiver_thread_freezes(tmp_path):
    host, target_agent, _, dispatch, state = _local_receiver(tmp_path)
    configuration = PeerConfigurationStore(host)
    configuration.initialize()
    service = PeerDeliveryService(host, configuration, Native(), dispatch, Waker())
    service.initialize()
    envelope = PeerEnvelope(
        id=uuid4(),
        organization_id=state["organization_id"],
        source_host=host.instance_id,
        source_agent=state["source_agent"],
        source_session="ses_sender",
        target_agent=target_agent,
        target_session="ses_source",
        text="Already accepted work",
    )
    accepted = asyncio.run(service.receive(str(host.instance_id), envelope))
    delivery = dispatch.get(state["organization_id"], str(target_agent), accepted["dispatch_id"])
    dispatch.change(delivery, "completed", validated=True, outcome={"kind": "settled"})
    host.begin_harness_switch(state["organization_id"], str(target_agent), 1, "codex")
    host.commit_freeze(
        state["organization_id"],
        str(target_agent),
        {
            "ses_source": {
                "runtime_type": "opencode",
                "session": {"id": "ses_source"},
                "history": [],
                "children": {},
            }
        },
    )

    assert asyncio.run(service.receive(str(host.instance_id), envelope)) == accepted


def test_terminal_peer_work_returns_one_result_to_its_original_source_thread(tmp_path):
    host = HostStore(
        ServiceSettings(
            service="agent-host",
            state_directory=tmp_path / "state",
            database_path=tmp_path / "state/host.sqlite3",
        )
    )
    host.initialize()
    organization_id = str(uuid4())
    host.bind_organization(organization_id, secrets.token_urlsafe(32))
    source_agent, target_agent = uuid4(), uuid4()
    for agent_id, name in ((source_agent, "Source"), (target_agent, "Target")):
        agent = HostAgentConfiguration(
            host_id=host.instance_id,
            organization_id=organization_id,
            agent_id=agent_id,
            version=1,
            name=name,
        )
        host.stage_agent(agent)
        host.mark_applied(agent)
    host.save_session(
        organization_id, str(source_agent), "ses_source", "/workspace/default/source", "Source"
    )
    configuration = PeerConfigurationStore(host)
    configuration.initialize()
    configuration.apply(
        PeerConfiguration(
            organization_id=organization_id,
            host_id=host.instance_id,
            version=1,
            agents=[
                PeerAgent(agent_id=source_agent, name="Source", host_id=host.instance_id),
                PeerAgent(agent_id=target_agent, name="Target", host_id=host.instance_id),
            ],
        )
    )
    dispatch = DispatchStore(host)
    dispatch.initialize()
    native = Native()
    native.histories["/session/ses_peer/message"] = [
        {
            "info": {"id": "msg_answer", "sessionID": "ses_peer", "role": "assistant"},
            "parts": [{"type": "text", "text": "The requested analysis is complete."}],
        }
    ]
    service = PeerDeliveryService(host, configuration, native, dispatch, Waker())
    service.initialize()
    request = PeerSend(id=uuid4(), target_agent=target_agent, text="Report when complete")
    asyncio.run(service.send(organization_id, str(source_agent), "ses_source", request))
    work = dispatch.get(organization_id, str(target_agent), str(request.id))
    assert dispatch.change(
        work, "completed", outcome={"kind": "native_run_completed", "message_id": "msg_answer"}
    )

    async def collect_results():
        async with service.run():
            for _ in range(250):
                first = dispatch.for_thread(organization_id, str(source_agent), "ses_source")
                if first:
                    break
                await asyncio.sleep(0.02)
            else:
                raise AssertionError("Timed out waiting for peer result")
            native.histories["/session/ses_peer/message"].append(
                {
                    "info": {"id": "msg_failed", "sessionID": "ses_peer", "role": "assistant"},
                    "parts": [],
                }
            )
            failed_request = PeerSend(
                id=uuid4(),
                target_agent=target_agent,
                target_session="ses_peer",
                text="Report failed work",
            )
            await service.send(organization_id, str(source_agent), "ses_source", failed_request)
            failed = dispatch.get(organization_id, str(target_agent), str(failed_request.id))
            assert dispatch.change(
                failed,
                "failed",
                outcome={"kind": "native_error", "message_id": "msg_failed"},
                error="Native provider rejected the request",
            )
            for _ in range(250):
                values = dispatch.for_thread(organization_id, str(source_agent), "ses_source")
                if len(values) == 2:
                    return first, values
                await asyncio.sleep(0.02)
        raise AssertionError("Timed out waiting for failed peer result")

    results, failed_results = asyncio.run(collect_results())
    assert len(results) == 1
    assert results[0]["payload"]["origin_id"] == str(request.id)
    assert results[0]["author"]["id"] == str(target_agent)
    assert results[0]["payload"]["text"] == "The requested analysis is complete."
    assert "Native provider rejected the request" in failed_results[1]["payload"]["text"]
    assert "msg_failed" in failed_results[1]["payload"]["text"]


class Native:
    def __init__(self):
        self.created = []
        self.histories = {}

    async def create_session(
        self, organization_id, agent_id, title, workspace, *, directory=None, metadata=None
    ):
        self.created.append((agent_id, directory, metadata))
        return {"id": "ses_peer"}

    async def request(
        self, organization_id, agent_id, path, *, method="GET", body=None, directory=None
    ):
        return self.histories.get(path, [])


class Waker:
    def __init__(self):
        self.woken = False

    def wake(self):
        self.woken = True


class CodexPeer:
    async def call(self, org, agent, method, params):
        if method != "thread/turns/list":
            raise AssertionError((method, params))
        return {
            "data": [
                {
                    "id": "turn_peer",
                    "status": "completed",
                    "items": [{"type": "agentMessage", "text": "Codex peer answer"}],
                }
            ],
            "nextCursor": None,
        }


class CodexPeerRouter:
    def __init__(self, codex):
        self.codex = codex

    def for_session(self, session):
        assert session["runtime_type"] == "codex"
        return self.codex


class MixedPeerRuntime(Native):
    def __init__(self, host):
        super().__init__()
        self.host = host
        self.codex = CodexPeer()
        self.runtime_router = CodexPeerRouter(self.codex)

    async def create_session(
        self, organization_id, agent_id, title, workspace, *, directory=None, metadata=None
    ):
        org, agent = organization_id, agent_id
        self.created.append((agent, directory, metadata))
        self.host.save_session(org, agent, "thr_peer", directory, title, runtime_type="codex")
        return {"id": "thr_peer", "directory": directory, "title": title}


def test_opencode_source_to_codex_peer_reserves_directory_and_returns_native_turn_text(tmp_path):
    org = str(uuid4())
    host = HostStore(
        ServiceSettings(
            service="agent-host",
            state_directory=tmp_path / "state",
            database_path=tmp_path / "host.sqlite3",
        )
    )
    host.initialize()
    host.bind_organization(org, secrets.token_urlsafe(32))
    source, target = uuid4(), uuid4()
    for agent, name, runtime_type in ((source, "Source", "opencode"), (target, "Target", "codex")):
        envelope = HostAgentConfiguration(
            host_id=host.instance_id,
            organization_id=org,
            agent_id=agent,
            version=1,
            name=name,
            configuration=AgentConfiguration(runtime_type=runtime_type),
        )
        host.stage_agent(envelope)
        host.mark_applied(envelope)
    host.save_session(org, str(source), "ses_source", "/workspace/default/source", "Source")
    configuration = PeerConfigurationStore(host)
    configuration.initialize()
    configuration.apply(
        PeerConfiguration(
            organization_id=org,
            host_id=host.instance_id,
            version=1,
            agents=[
                PeerAgent(agent_id=source, name="Source", host_id=host.instance_id),
                PeerAgent(agent_id=target, name="Target", host_id=host.instance_id),
            ],
        )
    )
    dispatch = DispatchStore(host)
    dispatch.initialize()
    runtime = MixedPeerRuntime(host)
    service = PeerDeliveryService(host, configuration, runtime, dispatch, Waker())
    service.initialize()
    request = PeerSend(id=uuid4(), target_agent=target, text="Analyze")

    asyncio.run(service.send(org, str(source), "ses_source", request))
    expected_directory = f"/workspace/default/threads/peer-{request.id.hex}"
    assert runtime.created == [
        (str(target), expected_directory, {"fesnyng_delivery_id": str(request.id)})
    ]
    delivery = dispatch.get(org, str(target), str(request.id))
    assert dispatch.change(
        delivery,
        "completed",
        outcome={"kind": "codex_turn_completed", "turn_id": "turn_peer"},
    )
    inbox = service._inbox(str(request.id))
    completed = dispatch.get(org, str(target), str(request.id))
    assert asyncio.run(service._result_text(inbox, completed)) == "Codex peer answer"
    asyncio.run(
        service._publish_result_once(
            inbox, PeerEnvelope.model_validate(json.loads(inbox["envelope"])), completed
        )
    )
    with host.connect() as connection:
        result = connection.execute(
            "SELECT envelope FROM peer_outbox WHERE id=?",
            (service._inbox(str(request.id))["result_id"],),
        ).fetchone()
    assert result is not None
    returned = json.loads(result["envelope"])
    assert returned["text"] == "Codex peer answer"
    assert returned["source_agent"] == str(target)
    assert returned["target_agent"] == str(source)


def test_remote_peer_delivery_uses_peer_auth_and_rejects_forged_source(tmp_path):
    organization_id = str(uuid4())
    source, source_agent = _host(tmp_path / "source", organization_id, "Source")
    target, target_agent = _host(tmp_path / "target", organization_id, "Target")
    source_host, target_host = source["host"], target["host"]
    source_token, target_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    source["configuration"].apply(
        PeerConfiguration(
            organization_id=organization_id,
            host_id=source_host.instance_id,
            version=1,
            agents=[
                PeerAgent(agent_id=source_agent, name="Source", host_id=source_host.instance_id),
                PeerAgent(agent_id=target_agent, name="Target", host_id=target_host.instance_id),
            ],
            peers=[
                PeerHost(
                    host_id=target_host.instance_id,
                    origin="http://target",
                    outbound_token=source_token,
                    inbound_token=target_token,
                )
            ],
        )
    )
    target["configuration"].apply(
        PeerConfiguration(
            organization_id=organization_id,
            host_id=target_host.instance_id,
            version=1,
            agents=[
                PeerAgent(agent_id=source_agent, name="Source", host_id=source_host.instance_id),
                PeerAgent(agent_id=target_agent, name="Target", host_id=target_host.instance_id),
            ],
            peers=[
                PeerHost(
                    host_id=source_host.instance_id,
                    origin="http://source",
                    outbound_token=target_token,
                    inbound_token=source_token,
                )
            ],
        )
    )
    target_app = FastAPI()
    target_app.state.peer_configuration = target["configuration"]
    target_app.state.peer_delivery = target["service"]
    target_app.include_router(router)

    async def transport(origin, token, envelope):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=target_app), base_url=origin
        ) as client:
            response = await client.post(
                f"/organizations/{organization_id}/peers/deliveries",
                headers={"Authorization": f"Bearer {token}"},
                json=envelope,
            )
            response.raise_for_status()
            return response.json()

    source["service"].transport = transport
    request = PeerSend(id=uuid4(), target_agent=target_agent, text="Remote peer work")
    receipt = asyncio.run(
        source["service"].send(organization_id, str(source_agent), "ses_source", request)
    )

    assert receipt["state"] == "accepted"
    target_delivery = target["dispatch"].get(organization_id, str(target_agent), str(request.id))
    assert target_delivery["author"] == {
        "kind": "agent",
        "id": str(source_agent),
        "name": "Source",
        "session_id": "ses_source",
    }

    forged = PeerEnvelope.model_validate(
        {
            **request.model_dump(mode="json"),
            "organization_id": organization_id,
            "source_host": str(target_host.instance_id),
            "source_agent": str(source_agent),
            "source_session": "ses_source",
        }
    )

    async def call_forged():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=target_app), base_url="http://target"
        ) as client:
            return await client.post(
                f"/organizations/{organization_id}/peers/deliveries",
                headers={"Authorization": f"Bearer {source_token}"},
                json=forged.model_dump(mode="json"),
            )

    assert asyncio.run(call_forged()).status_code == 403

    conflicted = request.model_copy(update={"text": "Changed after acceptance"})

    async def call_conflict():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=target_app), base_url="http://target"
        ) as client:
            return await client.post(
                f"/organizations/{organization_id}/peers/deliveries",
                headers={"Authorization": f"Bearer {source_token}"},
                json={
                    **conflicted.model_dump(mode="json"),
                    "organization_id": organization_id,
                    "source_host": str(source_host.instance_id),
                    "source_agent": str(source_agent),
                    "source_session": "ses_source",
                },
            )

    assert asyncio.run(call_conflict()).status_code == 409

    async def call_invalid_envelope():
        payload = {
            **request.model_dump(mode="json"),
            "organization_id": organization_id,
            "source_host": str(source_host.instance_id),
            "source_agent": str(source_agent),
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=target_app), base_url="http://target"
        ) as client:
            return await client.post(
                f"/organizations/{organization_id}/peers/deliveries",
                headers={"Authorization": f"Bearer {source_token}"},
                json=payload,
            )

    assert asyncio.run(call_invalid_envelope()).status_code == 422


def test_definitive_remote_rejection_is_terminal_and_notifies_the_source_thread(tmp_path):
    service, organization_id, source_agent, target_agent, dispatch = _remote_sender(tmp_path)
    calls = []

    async def rejected(origin, token, envelope):
        calls.append(envelope)
        raise _http_status_error(404)

    service.transport = rejected
    request = PeerSend(id=uuid4(), target_agent=target_agent, text="Use the wrong target thread")

    receipt = asyncio.run(service.send(organization_id, str(source_agent), "ses_source", request))

    assert receipt["state"] == "rejected"
    assert receipt["attempts"] == 1
    assert "HTTP 404" in receipt["error"]
    repeated = asyncio.run(service.send(organization_id, str(source_agent), "ses_source", request))
    assert repeated == receipt
    assert len(calls) == 1

    async def observe_worker():
        async with service.run():
            service.changed.set()
            await asyncio.sleep(0.05)

    asyncio.run(observe_worker())
    assert len(calls) == 1
    notices = dispatch.for_thread(organization_id, str(source_agent), "ses_source")
    assert len(notices) == 1
    assert notices[0]["payload"]["origin_id"] == str(request.id)
    assert notices[0]["author"] == {
        "kind": "agent",
        "id": str(target_agent),
        "name": "Target",
        "session_id": None,
    }
    assert "rejected before target acceptance" in notices[0]["payload"]["text"]


def test_duplicate_recovery_finishes_an_interrupted_rejection_notice(tmp_path, monkeypatch):
    service, organization_id, source_agent, target_agent, dispatch = _remote_sender(tmp_path)
    delivery_calls = []

    async def rejected(origin, token, envelope):
        delivery_calls.append(envelope)
        raise _http_status_error(404)

    service.transport = rejected
    request = PeerSend(id=uuid4(), target_agent=target_agent, text="Recover the result notice")
    enqueue = dispatch.enqueue

    def interrupted(*args, **kwargs):
        enqueue(*args, **kwargs)
        raise RuntimeError("interrupted after durable notice enqueue")

    monkeypatch.setattr(dispatch, "enqueue", interrupted)
    interrupted_receipt = asyncio.run(
        service.send(organization_id, str(source_agent), "ses_source", request)
    )

    assert interrupted_receipt["state"] == "rejected"
    assert interrupted_receipt["result_id"] is None
    monkeypatch.setattr(dispatch, "enqueue", enqueue)
    recovered = asyncio.run(service.send(organization_id, str(source_agent), "ses_source", request))
    assert recovered["state"] == "rejected"
    assert recovered["result_id"] is not None
    assert len(delivery_calls) == 1
    assert len(dispatch.for_thread(organization_id, str(source_agent), "ses_source")) == 1


@pytest.mark.parametrize("status_code", [408, 409, 429])
def test_ambiguous_remote_client_status_remains_pending(tmp_path, status_code):
    service, organization_id, source_agent, target_agent, _ = _remote_sender(tmp_path)

    async def ambiguous(origin, token, envelope):
        raise _http_status_error(status_code)

    service.transport = ambiguous
    request = PeerSend(id=uuid4(), target_agent=target_agent, text="Wait for a verified outcome")

    receipt = asyncio.run(service.send(organization_id, str(source_agent), "ses_source", request))

    assert receipt["state"] == "pending"
    assert receipt["attempts"] == 1


def test_later_rejection_after_an_uncertain_transport_outcome_stays_uncertain(tmp_path):
    service, organization_id, source_agent, target_agent, dispatch = _remote_sender(tmp_path)
    calls = []

    async def uncertain_then_rejected(origin, token, envelope):
        calls.append(envelope)
        if len(calls) == 1:
            raise httpx.ConnectError("response lost after the receiver may have accepted")
        raise _http_status_error(404)

    service.transport = uncertain_then_rejected
    request = PeerSend(
        id=uuid4(), target_agent=target_agent, text="Preserve an unknown first outcome"
    )
    initial = asyncio.run(service.send(organization_id, str(source_agent), "ses_source", request))
    assert initial["state"] == "pending"
    with service.store.connect() as connection:
        connection.execute("UPDATE peer_outbox SET next_attempt=0 WHERE id=?", (str(request.id),))

    async def retry_once():
        async with service.run():
            for _ in range(100):
                service.changed.set()
                receipt = service.status(organization_id, str(source_agent), str(request.id))
                if receipt["state"] == "uncertain":
                    return receipt
                await asyncio.sleep(0.01)
        raise AssertionError("Timed out waiting for the uncertain peer outcome")

    outcome = asyncio.run(retry_once())
    assert outcome["attempts"] == 2
    assert "outcome is uncertain" in outcome["error"]
    assert len(calls) == 2
    notices = dispatch.for_thread(organization_id, str(source_agent), "ses_source")
    assert len(notices) == 1
    assert notices[0]["payload"]["origin_id"] == str(request.id)
    assert "prior acceptance is unknown" in notices[0]["payload"]["text"]
    assert "rejected before target acceptance" not in notices[0]["payload"]["text"]
    repeated = asyncio.run(service.send(organization_id, str(source_agent), "ses_source", request))
    assert repeated["state"] == "uncertain"
    assert len(dispatch.for_thread(organization_id, str(source_agent), "ses_source")) == 1


def test_restart_404_after_an_interrupted_transport_attempt_stays_uncertain(tmp_path):
    service, organization_id, source_agent, target_agent, dispatch = _remote_sender(tmp_path)
    started = asyncio.Event()

    async def interrupted_after_remote_acceptance(origin, token, envelope):
        started.set()
        await asyncio.Event().wait()

    service.transport = interrupted_after_remote_acceptance
    request = PeerSend(
        id=uuid4(), target_agent=target_agent, text="Do not forget unknown acceptance"
    )

    async def interrupt_delivery():
        sending = asyncio.create_task(
            service.send(organization_id, str(source_agent), "ses_source", request)
        )
        await started.wait()
        sending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sending

    asyncio.run(interrupt_delivery())
    assert service.status(organization_id, str(source_agent), str(request.id))["attempts"] == 1
    recovered = PeerDeliveryService(service.store, service.config, Native(), dispatch, Waker())
    recovered.initialize()

    async def rejected_after_restart(origin, token, envelope):
        raise _http_status_error(404)

    recovered.transport = rejected_after_restart
    with recovered.store.connect() as connection:
        connection.execute("UPDATE peer_outbox SET next_attempt=0 WHERE id=?", (str(request.id),))

    async def wait_for_uncertainty():
        async with recovered.run():
            for _ in range(100):
                recovered.changed.set()
                receipt = recovered.status(organization_id, str(source_agent), str(request.id))
                if receipt["state"] == "uncertain":
                    return receipt
                await asyncio.sleep(0.01)
        raise AssertionError("Timed out waiting for the recovered uncertain outcome")

    receipt = asyncio.run(wait_for_uncertainty())
    assert receipt["attempts"] == 2
    assert "outcome is uncertain" in receipt["error"]


def test_legacy_pending_outbox_is_conservatively_uncertain_after_recovery(tmp_path):
    service, organization_id, source_agent, target_agent, _ = _remote_sender(tmp_path)
    request = PeerSend(
        id=uuid4(), target_agent=target_agent, text="Recover a legacy outbound delivery"
    )
    envelope = PeerEnvelope(
        **request.model_dump(),
        organization_id=organization_id,
        source_host=service.store.instance_id,
        source_agent=source_agent,
        source_session="ses_source",
    )
    target_host = service.config.agent(organization_id, str(target_agent))["host_id"]
    with service.store.connect() as connection:
        connection.execute("DROP TABLE peer_outbox")
        connection.execute(
            """CREATE TABLE peer_outbox (
                id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, source_agent TEXT NOT NULL,
                source_session TEXT NOT NULL, target_host TEXT NOT NULL, envelope TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', receipt TEXT, error TEXT,
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL DEFAULT (unixepoch()), updated_at INTEGER NOT NULL DEFAULT (unixepoch())
            )"""
        )
        connection.execute(
            "INSERT INTO peer_outbox(id,organization_id,source_agent,source_session,target_host,envelope) "
            "VALUES(?,?,?,?,?,?)",
            (
                str(request.id),
                organization_id,
                str(source_agent),
                "ses_source",
                target_host,
                envelope.model_dump_json(),
            ),
        )
    service.initialize()
    assert service.status(organization_id, str(source_agent), str(request.id))["attempts"] == 1

    async def rejected_after_restart(origin, token, envelope):
        raise _http_status_error(404)

    service.transport = rejected_after_restart

    async def wait_for_uncertainty():
        async with service.run():
            for _ in range(100):
                service.changed.set()
                receipt = service.status(organization_id, str(source_agent), str(request.id))
                if receipt["state"] == "uncertain":
                    return receipt
                await asyncio.sleep(0.01)
        raise AssertionError("Timed out waiting for the legacy uncertain outcome")

    receipt = asyncio.run(wait_for_uncertainty())
    assert receipt["attempts"] == 2
    assert "outcome is uncertain" in receipt["error"]


def test_legacy_outbox_schema_and_conservative_backfill_rollback_together(tmp_path):
    service, organization_id, source_agent, target_agent, _ = _remote_sender(tmp_path)
    request = PeerSend(id=uuid4(), target_agent=target_agent, text="Protect the atomic migration")
    envelope = PeerEnvelope(
        **request.model_dump(),
        organization_id=organization_id,
        source_host=service.store.instance_id,
        source_agent=source_agent,
        source_session="ses_source",
    )
    target_host = service.config.agent(organization_id, str(target_agent))["host_id"]
    with service.store.connect() as connection:
        connection.execute("DROP TABLE peer_outbox")
        connection.execute(
            """CREATE TABLE peer_outbox (
                id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, source_agent TEXT NOT NULL,
                source_session TEXT NOT NULL, target_host TEXT NOT NULL, envelope TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', receipt TEXT, error TEXT,
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL DEFAULT (unixepoch()), updated_at INTEGER NOT NULL DEFAULT (unixepoch())
            )"""
        )
        connection.execute(
            "INSERT INTO peer_outbox(id,organization_id,source_agent,source_session,target_host,envelope) "
            "VALUES(?,?,?,?,?,?)",
            (
                str(request.id),
                organization_id,
                str(source_agent),
                "ses_source",
                target_host,
                envelope.model_dump_json(),
            ),
        )
        connection.execute(
            """CREATE TRIGGER fail_peer_outbox_backfill
            BEFORE UPDATE OF attempts ON peer_outbox
            WHEN OLD.attempts = 0 AND NEW.attempts = 1
            BEGIN SELECT RAISE(ABORT, 'forced legacy backfill failure'); END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced legacy backfill failure"):
        service.initialize()

    with service.store.connect() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(peer_outbox)")}
        attempts = connection.execute(
            "SELECT attempts FROM peer_outbox WHERE id=?", (str(request.id),)
        ).fetchone()["attempts"]
        connection.execute("DROP TRIGGER fail_peer_outbox_backfill")
    assert "result_id" not in columns
    assert attempts == 0

    service.initialize()
    assert service.status(organization_id, str(source_agent), str(request.id))["attempts"] == 1


def test_direct_send_and_retry_worker_share_one_outbound_delivery_attempt(tmp_path):
    service, organization_id, source_agent, target_agent, dispatch = _remote_sender(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def accepted(origin, token, envelope):
        calls.append(envelope)
        started.set()
        service.changed.set()
        await release.wait()
        return {
            "id": str(envelope["id"]),
            "organization_id": envelope["organization_id"],
            "source_host": envelope["source_host"],
            "source_agent": envelope["source_agent"],
            "source_session": envelope["source_session"],
            "target_host": str(target_host),
            "target_agent": envelope["target_agent"],
            "target_session": "ses_target",
            "dispatch_id": str(envelope["id"]),
            "state": "accepted",
        }

    target_host = service.config.agent(organization_id, str(target_agent))["host_id"]
    service.transport = accepted
    request = PeerSend(id=uuid4(), target_agent=target_agent, text="Serialize this one delivery")

    async def race():
        async with service.run():
            direct = asyncio.create_task(
                service.send(organization_id, str(source_agent), "ses_source", request)
            )
            await started.wait()
            await asyncio.sleep(0.05)
            release.set()
            return await direct

    receipt = asyncio.run(race())
    assert receipt["state"] == "accepted"
    assert len(calls) == 1
    assert dispatch.for_thread(organization_id, str(source_agent), "ses_source") == []


def test_transient_peer_outage_remains_pending_for_a_later_retry(tmp_path):
    service, organization_id, source_agent, target_agent, _ = _remote_sender(tmp_path)
    calls = []

    async def unavailable(origin, token, envelope):
        calls.append(envelope)
        raise httpx.ConnectError("network unavailable")

    service.transport = unavailable
    request = PeerSend(id=uuid4(), target_agent=target_agent, text="Retry after the outage")

    initial = asyncio.run(service.send(organization_id, str(source_agent), "ses_source", request))

    assert initial["state"] == "pending"
    assert initial["attempts"] == 1
    with service.store.connect() as connection:
        connection.execute("UPDATE peer_outbox SET next_attempt=0 WHERE id=?", (str(request.id),))

    async def retry_once():
        async with service.run():
            for _ in range(100):
                service.changed.set()
                receipt = service.status(organization_id, str(source_agent), str(request.id))
                if receipt["attempts"] == 2:
                    return receipt
                await asyncio.sleep(0.01)
        raise AssertionError("Timed out waiting for the transient peer retry")

    retried = asyncio.run(retry_once())
    assert retried["state"] == "pending"
    assert retried["attempts"] == 2
    assert len(calls) == 2
    assert calls[1] == calls[0]


def test_local_unknown_target_thread_is_rejected_before_native_effects(tmp_path):
    host, target_agent, configuration, dispatch, service_state = _local_receiver(tmp_path)
    native = Native()
    service = PeerDeliveryService(host, configuration, native, dispatch, Waker())
    service.initialize()
    request = PeerSend(
        id=uuid4(),
        target_agent=target_agent,
        target_session="ses_wrong_host_thread",
        text="This target thread is not on this host",
    )

    receipt = asyncio.run(
        service.send(
            service_state["organization_id"],
            str(service_state["source_agent"]),
            "ses_sender",
            request,
        )
    )

    assert receipt["state"] == "rejected"
    assert receipt["attempts"] == 1
    assert native.created == []
    assert dispatch.for_thread(
        service_state["organization_id"], str(service_state["source_agent"]), "ses_sender"
    )[0]["payload"]["origin_id"] == str(request.id)


def test_lost_native_create_response_adopts_only_metadata_correlated_session(tmp_path):
    host, agent, configuration, dispatch, service = _local_receiver(tmp_path)
    delivery_id, source_agent = uuid4(), service["source_agent"]
    envelope = PeerEnvelope(
        id=delivery_id,
        organization_id=service["organization_id"],
        source_host=host.instance_id,
        source_agent=source_agent,
        source_session="ses_source",
        target_agent=agent,
        text="Recover this",
    )
    native = LostResponseNative(delivery_id)
    receiver = PeerDeliveryService(host, configuration, native, dispatch, Waker())
    receiver.initialize()

    receipt = asyncio.run(receiver.receive(str(host.instance_id), envelope))

    assert receipt["state"] == "accepted"
    assert native.creates == 1
    assert native.lookups == 1
    assert (
        dispatch.get(service["organization_id"], str(agent), str(delivery_id))["session_id"]
        == "ses_adopted"
    )
    with pytest.raises(PermissionError):
        receiver.received(str(uuid4()), str(host.instance_id), str(delivery_id))


def test_real_runtime_session_persistence_is_reused_by_peer_reservation(tmp_path):
    host, agent, configuration, dispatch, service = _local_receiver(tmp_path)
    delivery_id, source_agent = uuid4(), service["source_agent"]
    envelope = PeerEnvelope(
        id=delivery_id,
        organization_id=service["organization_id"],
        source_host=host.instance_id,
        source_agent=source_agent,
        source_session="ses_source",
        target_agent=agent,
        text="Native persistence",
    )
    runtime = NativeBoundaryRuntime(host)
    receiver = PeerDeliveryService(host, configuration, runtime, dispatch, Waker())
    receiver.initialize()

    receipt = asyncio.run(receiver.receive(str(host.instance_id), envelope))

    assert receipt["state"] == "accepted"
    saved = host.session(service["organization_id"], str(agent), "ses_runtime")
    assert saved["directory"] == f"/workspace/default/threads/peer-{delivery_id.hex}"
    assert runtime.session_posts == [
        {
            "title": "Peer collaboration",
            "permission": [{"permission": "*", "pattern": "*", "action": "allow"}],
            "metadata": {"fesnyng_delivery_id": str(delivery_id)},
        }
    ]


def test_unmatched_lost_native_create_stays_uncertain_without_replaying_create(tmp_path):
    host, agent, configuration, dispatch, service = _local_receiver(tmp_path)
    delivery_id, source_agent = uuid4(), service["source_agent"]
    envelope = PeerEnvelope(
        id=delivery_id,
        organization_id=service["organization_id"],
        source_host=host.instance_id,
        source_agent=source_agent,
        source_session="ses_source",
        target_agent=agent,
        text="Do not replay",
    )
    native = LostResponseNative(None)
    receiver = PeerDeliveryService(host, configuration, native, dispatch, Waker())
    receiver.initialize()

    with pytest.raises(RuntimeUnavailable):
        asyncio.run(receiver.receive(str(host.instance_id), envelope))
    assert (
        receiver.received(service["organization_id"], str(host.instance_id), str(delivery_id))[
            "state"
        ]
        == "uncertain"
    )
    assert asyncio.run(receiver.receive(str(host.instance_id), envelope))["state"] == "uncertain"
    assert native.creates == 1


def test_same_host_uncertain_reservation_stays_pending_then_retries_after_metadata_appears(
    tmp_path,
):
    host, target_agent, configuration, dispatch, service_state = _local_receiver(tmp_path)
    source_agent = service_state["source_agent"]
    delivery_id = uuid4()
    native = LostResponseNative(None)
    service = PeerDeliveryService(host, configuration, native, dispatch, Waker())
    service.initialize()
    request = PeerSend(id=delivery_id, target_agent=target_agent, text="Wait for native evidence")

    initial = asyncio.run(
        service.send(service_state["organization_id"], str(source_agent), "ses_sender", request)
    )

    assert initial["state"] == "pending"
    assert native.creates == 1
    native.delivery_id = delivery_id

    async def wait_for_acceptance():
        async with service.run():
            for _ in range(200):
                receipt = service.status(
                    service_state["organization_id"], str(source_agent), str(delivery_id)
                )
                if receipt["state"] == "accepted":
                    return receipt
                await asyncio.sleep(0.02)
        raise AssertionError("Timed out waiting for metadata-correlated local retry")

    accepted = asyncio.run(wait_for_acceptance())
    assert accepted["state"] == "accepted"
    assert native.creates == 1
    assert (
        dispatch.get(service_state["organization_id"], str(target_agent), str(delivery_id))["state"]
        == "queued"
    )


def test_restart_resend_adopts_one_exact_reserved_native_session_then_enqueues(tmp_path):
    host, agent, configuration, dispatch, service = _local_receiver(tmp_path)
    delivery_id, source_agent = uuid4(), service["source_agent"]
    envelope = PeerEnvelope(
        id=delivery_id,
        organization_id=service["organization_id"],
        source_host=host.instance_id,
        source_agent=source_agent,
        source_session="ses_source",
        target_agent=agent,
        text="Resume after restart",
    )
    before_restart = PeerDeliveryService(host, configuration, Native(), dispatch, Waker())
    before_restart.initialize()
    with host.connect() as connection:
        connection.execute(
            "INSERT INTO peer_inbox(id,organization_id,source_host,source_agent,source_session,"
            "target_agent,directory,envelope,author,state) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                str(delivery_id),
                service["organization_id"],
                str(host.instance_id),
                str(source_agent),
                "ses_source",
                str(agent),
                f"/workspace/default/threads/peer-{delivery_id.hex}",
                envelope.model_dump_json(),
                Actor(
                    kind="agent", id=source_agent, name="Source", session_id="ses_source"
                ).model_dump_json(),
                "creating",
            ),
        )
    configuration.apply(
        PeerConfiguration(
            organization_id=service["organization_id"],
            host_id=host.instance_id,
            version=2,
            agents=[
                PeerAgent(agent_id=source_agent, name="Renamed source", host_id=host.instance_id),
                PeerAgent(agent_id=agent, name="Target", host_id=host.instance_id),
            ],
        )
    )
    native = LostResponseNative(delivery_id)
    after_restart = PeerDeliveryService(host, configuration, native, dispatch, Waker())
    after_restart.initialize()

    receipt = asyncio.run(after_restart.receive(str(host.instance_id), envelope))

    assert receipt["state"] == "accepted"
    assert native.creates == 0
    assert native.lookups == 1
    recovered = dispatch.get(service["organization_id"], str(agent), str(delivery_id))
    assert recovered["state"] == "queued"
    assert recovered["author"]["name"] == "Source"


def test_forked_native_metadata_cannot_be_adopted_after_restart(tmp_path):
    host, agent, configuration, dispatch, service = _local_receiver(tmp_path)
    delivery_id, source_agent = uuid4(), service["source_agent"]
    envelope = PeerEnvelope(
        id=delivery_id,
        organization_id=service["organization_id"],
        source_host=host.instance_id,
        source_agent=source_agent,
        source_session="ses_source",
        target_agent=agent,
        text="Reject forked metadata",
    )
    receiver = PeerDeliveryService(
        host, configuration, ForkedNative(delivery_id), dispatch, Waker()
    )
    receiver.initialize()
    with host.connect() as connection:
        connection.execute(
            "INSERT INTO peer_inbox(id,organization_id,source_host,source_agent,source_session,"
            "target_agent,directory,envelope,state) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                str(delivery_id),
                service["organization_id"],
                str(host.instance_id),
                str(source_agent),
                "ses_source",
                str(agent),
                f"/workspace/default/threads/peer-{delivery_id.hex}",
                envelope.model_dump_json(),
                "creating",
            ),
        )

    receipt = asyncio.run(receiver.receive(str(host.instance_id), envelope))

    assert receipt["state"] == "uncertain"
    with pytest.raises(LookupError):
        dispatch.get(service["organization_id"], str(agent), str(delivery_id))


def test_concurrent_duplicate_receives_share_one_native_reservation(tmp_path):
    host, agent, configuration, dispatch, service = _local_receiver(tmp_path)
    delivery_id, source_agent = uuid4(), service["source_agent"]
    envelope = PeerEnvelope(
        id=delivery_id,
        organization_id=service["organization_id"],
        source_host=host.instance_id,
        source_agent=source_agent,
        source_session="ses_source",
        target_agent=agent,
        text="Serialize duplicates",
    )
    native = BlockingNative()
    receiver = PeerDeliveryService(host, configuration, native, dispatch, Waker())
    receiver.initialize()

    async def deliver_twice():
        first = asyncio.create_task(receiver.receive(str(host.instance_id), envelope))
        await native.started.wait()
        second = asyncio.create_task(receiver.receive(str(host.instance_id), envelope))
        await asyncio.sleep(0)
        assert native.creates == 1
        native.release.set()
        return await asyncio.gather(first, second)

    first, second = asyncio.run(deliver_twice())
    assert first == second
    assert first["target_session"] == "ses_peer"
    assert native.creates == 1


def _remote_sender(path):
    organization_id = str(uuid4())
    source, source_agent = _host(path / "source", organization_id, "Source")
    target_agent, target_host = uuid4(), uuid4()
    source["configuration"].apply(
        PeerConfiguration(
            organization_id=organization_id,
            host_id=source["host"].instance_id,
            version=1,
            agents=[
                PeerAgent(agent_id=source_agent, name="Source", host_id=source["host"].instance_id),
                PeerAgent(agent_id=target_agent, name="Target", host_id=target_host),
            ],
            peers=[
                PeerHost(
                    host_id=target_host,
                    origin="http://target",
                    outbound_token=secrets.token_urlsafe(32),
                    inbound_token=secrets.token_urlsafe(32),
                )
            ],
        )
    )
    return source["service"], organization_id, source_agent, target_agent, source["dispatch"]


def _http_status_error(status_code):
    request = httpx.Request("POST", "http://target/peers/deliveries")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(
        "peer receiver rejected the envelope", request=request, response=response
    )


def _host(path, organization_id, name):
    host = HostStore(
        ServiceSettings(
            service="agent-host",
            state_directory=path / "state",
            database_path=path / "state/host.sqlite3",
        )
    )
    host.initialize()
    host.bind_organization(organization_id, secrets.token_urlsafe(32))
    agent = uuid4()
    configuration = PeerConfigurationStore(host)
    configuration.initialize()
    dispatch = DispatchStore(host)
    dispatch.initialize()
    service = PeerDeliveryService(host, configuration, Native(), dispatch, Waker())
    service.initialize()
    envelope = HostAgentConfiguration(
        host_id=host.instance_id,
        organization_id=organization_id,
        agent_id=agent,
        version=1,
        name=name,
    )
    host.stage_agent(envelope)
    host.mark_applied(envelope)
    host.save_session(organization_id, str(agent), "ses_source", "/workspace/default/source", name)
    return {
        "host": host,
        "configuration": configuration,
        "dispatch": dispatch,
        "service": service,
    }, agent


def _local_receiver(path):
    organization_id = str(uuid4())
    state, target_agent = _host(path, organization_id, "Target")
    source_agent = uuid4()
    source = HostAgentConfiguration(
        host_id=state["host"].instance_id,
        organization_id=organization_id,
        agent_id=source_agent,
        version=1,
        name="Source",
    )
    state["host"].stage_agent(source)
    state["host"].mark_applied(source)
    state["host"].save_session(
        organization_id, str(source_agent), "ses_sender", "/workspace/default/source", "Source"
    )
    state["configuration"].apply(
        PeerConfiguration(
            organization_id=organization_id,
            host_id=state["host"].instance_id,
            version=1,
            agents=[
                PeerAgent(agent_id=source_agent, name="Source", host_id=state["host"].instance_id),
                PeerAgent(agent_id=target_agent, name="Target", host_id=state["host"].instance_id),
            ],
        )
    )
    return (
        state["host"],
        target_agent,
        state["configuration"],
        state["dispatch"],
        {
            "organization_id": organization_id,
            "source_agent": source_agent,
        },
    )


class LostResponseNative:
    def __init__(self, delivery_id):
        self.delivery_id = delivery_id
        self.creates = 0
        self.lookups = 0

    async def create_session(self, *args, **kwargs):
        self.creates += 1
        raise RuntimeUnavailable("connection reset after native accepted creation")

    async def request(self, *args, **kwargs):
        self.lookups += 1
        if self.delivery_id is None:
            return []
        return [
            {
                "id": "ses_adopted",
                "directory": kwargs["directory"],
                "metadata": {"fesnyng_delivery_id": str(self.delivery_id)},
            }
        ]


class BlockingNative(Native):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.creates = 0

    async def create_session(self, *args, **kwargs):
        self.creates += 1
        self.started.set()
        await self.release.wait()
        return {"id": "ses_peer"}


class ForkedNative(LostResponseNative):
    async def request(self, *args, **kwargs):
        return [
            {
                "id": "ses_child",
                "directory": kwargs["directory"],
                "parentID": "ses_parent",
                "metadata": {"fesnyng_delivery_id": str(self.delivery_id)},
            }
        ]


class NativeBoundaryRuntime(DockerRuntime):
    def __init__(self, host):
        super().__init__(host, "http://credentials.invalid")
        self.session_posts = []

    async def docker(self, *args, content=None):
        return b""

    async def request(
        self, organization_id, agent_id, path, *, method="GET", body=None, directory=None
    ):
        assert path == "/session"
        assert method == "POST"
        self.session_posts.append(body)
        return {"id": "ses_runtime"}
