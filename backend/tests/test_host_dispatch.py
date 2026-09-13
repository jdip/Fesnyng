import asyncio
from typing import cast
from uuid import uuid4

import pytest

from fesnyng_backend.host_dispatch import Dispatcher, DispatchStore, Submission
from fesnyng_backend.host_effects import native_tools_running, native_tools_settled
from fesnyng_backend.host_interactions import Interactions
from fesnyng_backend.host_models import Actor, HostAgentConfiguration
from fesnyng_backend.host_runtime import RuntimeUnavailable
from fesnyng_backend.host_store import HostStore
from fesnyng_backend.settings import ServiceSettings


def test_dispatch_receipts_are_durable_ordered_and_reject_conflicting_retries(tmp_path):
    host = HostStore(
        ServiceSettings(
            service="agent-host",
            state_directory=tmp_path / "state",
            database_path=tmp_path / "state/host.sqlite3",
        )
    )
    host.initialize()
    org, agent = str(uuid4()), str(uuid4())
    host.bind_organization(org, "test organization binding with enough characters")
    host.stage_agent(
        HostAgentConfiguration(
            host_id=host.instance_id,
            organization_id=org,
            agent_id=agent,
            version=1,
            name="Engineer",
        )
    )
    host.save_session(org, agent, "ses_thread", "/workspace/default/thread", "Thread")
    deliveries = DispatchStore(host)
    deliveries.initialize()
    author = Actor(kind="human", id=uuid4(), name="Owner")
    first = Submission(id=uuid4(), text="Inspect the repository")
    receipt = deliveries.enqueue(org, agent, "ses_thread", first, author)
    assert receipt["sequence"] == 1
    assert receipt["state"] == "queued"
    assert receipt["author"]["id"] == str(author.id)
    assert deliveries.enqueue(org, agent, "ses_thread", first, author) == receipt
    second = deliveries.enqueue(
        org, agent, "ses_thread", Submission(id=uuid4(), text="Report the result"), author
    )
    assert second["sequence"] == 2
    restored = DispatchStore(HostStore(host.settings))
    assert restored.get(org, agent, str(first.id)) == receipt
    with pytest.raises(ValueError, match="conflict"):
        deliveries.enqueue(
            org, agent, "ses_thread", first.model_copy(update={"text": "Different request"}), author
        )
    with pytest.raises(LookupError):
        restored.get(str(uuid4()), agent, str(first.id))
    with pytest.raises(LookupError):
        deliveries.enqueue(
            org, agent, "ses_wrong_thread", Submission(id=uuid4(), text="Wrong target"), author
        )


def test_native_delivery_is_fifo_per_thread_but_threads_run_concurrently(tmp_path):
    host = HostStore(
        ServiceSettings(
            service="agent-host",
            state_directory=tmp_path / "state",
            database_path=tmp_path / "state/host.sqlite3",
        )
    )
    host.initialize()
    org, agent = str(uuid4()), str(uuid4())
    host.bind_organization(org, "test organization binding with enough characters")
    envelope = HostAgentConfiguration(
        host_id=host.instance_id, organization_id=org, agent_id=agent, version=1, name="Engineer"
    )
    host.stage_agent(envelope)
    host.mark_applied(envelope)
    for session in ("ses_one", "ses_two"):
        host.save_session(org, agent, session, f"/workspace/{session}", session)
    store = DispatchStore(host)
    store.initialize()
    actor = Actor(kind="human", id=uuid4(), name="Owner")
    first = store.enqueue(org, agent, "ses_one", Submission(id=uuid4(), text="First"), actor)
    second = store.enqueue(org, agent, "ses_one", Submission(id=uuid4(), text="Second"), actor)
    other = store.enqueue(org, agent, "ses_two", Submission(id=uuid4(), text="Other"), actor)

    async def check():
        native = Native()
        runner = Dispatcher(store, native)
        async with runner.run():
            for _ in range(100):
                if len(native.received) == 2:
                    break
                await asyncio.sleep(0.01)
            assert set(native.received) == {"First", "Other"}
            assert store.get(org, agent, second["id"])["state"] == "queued"
            native.finish("ses_one")
            runner.wake()
            for _ in range(100):
                if "Second" in native.received:
                    break
                await asyncio.sleep(0.01)
            assert native.received.index("First") < native.received.index("Second")
            assert store.get(org, agent, first["id"])["state"] == "completed"
            assert store.get(org, agent, other["id"])["state"] in {"submitting", "active"}

    asyncio.run(check())


def test_stalled_thread_probe_does_not_block_healthy_thread_fifo(tmp_path):
    host = HostStore(
        ServiceSettings(
            service="agent-host",
            state_directory=tmp_path / "state",
            database_path=tmp_path / "state/host.sqlite3",
        )
    )
    host.initialize()
    org, agent = str(uuid4()), str(uuid4())
    host.bind_organization(org, "test organization binding with enough characters")
    envelope = HostAgentConfiguration(
        host_id=host.instance_id, organization_id=org, agent_id=agent, version=1, name="Engineer"
    )
    host.stage_agent(envelope)
    host.mark_applied(envelope)
    host.save_session(org, agent, "ses_healthy", "/workspace/ses_healthy", "Healthy")
    host.save_session(org, agent, "ses_stalled", "/workspace/ses_stalled", "Stalled")
    store = DispatchStore(host)
    store.initialize()
    author = Actor(kind="human", id=uuid4(), name="Owner")

    class StalledStatus(Native):
        def __init__(self):
            super().__init__()
            self.messages["ses_healthy"] = []
            self.messages["ses_stalled"] = []
            self.finished["ses_healthy"] = asyncio.Event()
            self.finished["ses_stalled"] = asyncio.Event()
            self.stalled = asyncio.Event()
            self.release = asyncio.Event()

        async def request(
            self, organization_id, agent_id, path, *, method="GET", body=None, directory=None
        ):
            if path == "/session/status" and directory == "/workspace/ses_stalled":
                self.stalled.set()
                await self.release.wait()
            return await super().request(
                organization_id, agent_id, path, method=method, body=body, directory=directory
            )

    async def check():
        native = StalledStatus()
        first = store.enqueue(
            org, agent, "ses_healthy", Submission(id=uuid4(), text="First"), author
        )
        second = store.enqueue(
            org, agent, "ses_healthy", Submission(id=uuid4(), text="Second"), author
        )
        store.enqueue(org, agent, "ses_stalled", Submission(id=uuid4(), text="Blocked"), author)
        runner = Dispatcher(store, native)
        async with runner.run():
            await native.stalled.wait()
            await eventually(lambda: native.received == ["First"])
            native.finish("ses_healthy")
            runner.wake()
            await eventually(lambda: native.received == ["First", "Second"])
            assert store.get(org, agent, first["id"])["state"] == "completed"
            assert store.get(org, agent, second["id"])["state"] in {"submitting", "active"}

    asyncio.run(check())


def test_completed_probe_waits_for_the_supervisor_poll_cadence(tmp_path):
    org, agent, store, _, author = interaction_system(tmp_path)
    receipt = store.enqueue(org, agent, "ses_one", Submission(id=uuid4(), text="Unknown"), author)
    assert store.change(receipt, "submitting", message_id="msg_unknown")

    class CountingNative(Native):
        def __init__(self):
            super().__init__()
            self.requests = 0

        async def request(
            self, organization_id, agent_id, path, *, method="GET", body=None, directory=None
        ):
            self.requests += 1
            return await super().request(
                organization_id, agent_id, path, method=method, body=body, directory=directory
            )

    async def check():
        native = CountingNative()
        async with Dispatcher(store, native).run():
            await asyncio.sleep(0.1)
            assert native.requests == 2

    asyncio.run(check())


class Native:
    def __init__(self):
        self.messages = {"ses_one": [], "ses_two": []}
        self.busy = set()
        self.received = []
        self.locks = {}
        self.aborts = 0
        self.finished = {session: asyncio.Event() for session in self.messages}

    def lock(self, agent_id):
        return self.locks.setdefault(agent_id, asyncio.Lock())

    async def request(
        self, organization_id, agent_id, path, *, method="GET", body=None, directory=None
    ):
        assert directory is not None
        session = directory.rsplit("/", 1)[-1]
        if path == "/session/status":
            return {session: {"type": "busy"}} if session in self.busy else {}
        if path.endswith("/message") and method == "POST":
            assert body is not None
            self.received.append(body["parts"][0]["text"])
            if session not in self.busy:
                self.finished[session] = asyncio.Event()
                self.busy.add(session)
            self.messages[session].append(
                {
                    "info": {"id": body["messageID"], "sessionID": session, "role": "user"},
                    "parts": body["parts"],
                }
            )
            await self.finished[session].wait()
            return None
        if path.endswith("/message") and method == "GET":
            return self.messages[session]
        if path.endswith("/abort"):
            self.aborts += 1
            if session in self.busy:
                self.finish(session, aborted=True)
            return True
        raise AssertionError(path)

    def finish(self, session, *, aborted=False):
        parent = self.messages[session][-1]["info"]["id"]
        self.messages[session].append(
            {
                "info": {
                    "id": "msg_result",
                    "sessionID": session,
                    "role": "assistant",
                    "parentID": parent,
                    "finish": "stop",
                    "time": {"completed": 1},
                    **({"error": {"name": "MessageAbortedError"}} if aborted else {}),
                },
                "parts": [{"type": "text", "text": "Done"}],
            }
        )
        self.busy.remove(session)
        self.finished[session].set()


def interaction_system(tmp_path):
    host = HostStore(
        ServiceSettings(
            service="agent-host",
            state_directory=tmp_path / "state",
            database_path=tmp_path / "state/host.sqlite3",
        )
    )
    host.initialize()
    org, agent = str(uuid4()), str(uuid4())
    host.bind_organization(org, "test organization binding with enough characters")
    envelope = HostAgentConfiguration(
        host_id=host.instance_id, organization_id=org, agent_id=agent, version=1, name="Engineer"
    )
    host.stage_agent(envelope)
    host.mark_applied(envelope)
    host.save_session(org, agent, "ses_one", "/workspace/ses_one", "Thread")
    store = DispatchStore(host)
    store.initialize()
    return org, agent, store, Native(), Actor(kind="human", id=uuid4(), name="Owner")


async def eventually(predicate):
    for _ in range(150):
        if predicate():
            return
        await asyncio.sleep(0.01)
    assert predicate(), "Expected native delivery state did not arrive"


def test_steering_joins_native_run_without_claiming_separate_fulfillment(tmp_path):
    org, agent, store, native, author = interaction_system(tmp_path)
    first = store.enqueue(org, agent, "ses_one", Submission(id=uuid4(), text="First"), author)

    async def check():
        runner = Dispatcher(store, native)
        async with runner.run():
            await eventually(lambda: native.received == ["First"])
            steering = store.enqueue(
                org,
                agent,
                "ses_one",
                Submission(id=uuid4(), text="Change direction", mode="steering"),
                author,
            )
            queued = store.enqueue(
                org, agent, "ses_one", Submission(id=uuid4(), text="Later"), author
            )
            runner.wake()
            await eventually(lambda: "Change direction" in native.received)
            assert "Later" not in native.received
            native.finish("ses_one")
            runner.wake()
            await eventually(lambda: "Later" in native.received)
            assert store.get(org, agent, first["id"])["state"] == "contributed"
            assert store.get(org, agent, steering["id"])["state"] == "completed"
            assert (
                store.get(org, agent, first["id"])["outcome"]["responding_to"]
                == store.get(org, agent, steering["id"])["native_message_id"]
            )
            assert store.get(org, agent, queued["id"])["state"] != "completed"

    asyncio.run(check())


def test_completed_steering_is_reconciled_before_its_earlier_contribution(tmp_path, monkeypatch):
    org, agent, store, native, author = interaction_system(tmp_path)
    first = store.enqueue(org, agent, "ses_one", Submission(id=uuid4(), text="First"), author)
    steering = store.enqueue(
        org,
        agent,
        "ses_one",
        Submission(id=uuid4(), text="Steer", mode="steering"),
        author,
    )
    monkeypatch.setattr("fesnyng_backend.host_dispatch.time.time", lambda: 1.0)
    assert store.change(first, "submitting", message_id="msg_first")
    assert store.change(steering, "submitting", message_id="msg_steer")
    native.messages["ses_one"] = [
        {
            "info": {"id": "msg_first", "sessionID": "ses_one", "role": "user"},
            "parts": [{"type": "text", "text": "First"}],
        },
        {
            "info": {"id": "msg_steer", "sessionID": "ses_one", "role": "user"},
            "parts": [{"type": "text", "text": "Steer"}],
        },
        {
            "info": {
                "id": "msg_result",
                "sessionID": "ses_one",
                "role": "assistant",
                "parentID": "msg_steer",
                "finish": "stop",
                "time": {"completed": 1},
            },
            "parts": [{"type": "text", "text": "Done"}],
        },
    ]
    monkeypatch.setattr("fesnyng_backend.host_dispatch.time.time", lambda: 10.0)

    asyncio.run(Dispatcher(store, native).step())

    assert store.get(org, agent, steering["id"])["state"] == "completed"
    assert store.get(org, agent, first["id"])["state"] == "contributed"


def test_explicit_stop_is_attributed_and_only_cancels_queued_work_when_requested(tmp_path):
    org, agent, store, native, author = interaction_system(tmp_path)
    first = store.enqueue(org, agent, "ses_one", Submission(id=uuid4(), text="Running"), author)

    async def check():
        runner = Dispatcher(store, native)
        async with runner.run():
            await eventually(lambda: native.received == ["Running"])
            queued = store.enqueue(
                org, agent, "ses_one", Submission(id=uuid4(), text="Later"), author
            )
            stop = store.enqueue(
                org,
                agent,
                "ses_one",
                Submission(id=uuid4(), mode="stop", cancel_queued=True),
                author,
            )
            runner.wake()
            await eventually(lambda: store.get(org, agent, stop["id"])["state"] == "completed")
            assert native.aborts == 1
            assert store.get(org, agent, queued["id"])["state"] == "cancelled"
            assert store.get(org, agent, stop["id"])["author"]["id"] == str(author.id)
            assert store.get(org, agent, stop["id"])["outcome"]["kind"] == "abort_acknowledged"
            runner.wake()
            await eventually(lambda: store.get(org, agent, first["id"])["state"] == "failed")
            assert native.received == ["Running"]

    asyncio.run(check())


def test_restart_reconciles_completed_native_work_without_resending_and_resumes_queue(tmp_path):
    org, agent, store, native, author = interaction_system(tmp_path)
    first = store.enqueue(
        org, agent, "ses_one", Submission(id=uuid4(), text="Already delivered"), author
    )
    store.enqueue(org, agent, "ses_one", Submission(id=uuid4(), text="Still queued"), author)

    async def check():
        async with Dispatcher(store, native).run():
            await eventually(lambda: native.received == ["Already delivered"])
        native.finish("ses_one")
        restored = DispatchStore(HostStore(store.host.settings))
        async with Dispatcher(restored, native).run():
            await eventually(lambda: "Still queued" in native.received)
            assert restored.get(org, agent, first["id"])["state"] == "completed"
            assert native.received.count("Already delivered") == 1

    asyncio.run(check())


@pytest.mark.parametrize("partial", [False, True])
def test_missing_or_partial_native_receipt_is_not_replayed_after_restart(
    tmp_path, monkeypatch, partial
):
    org, agent, store, _, author = interaction_system(tmp_path)

    class Interrupted(Native):
        async def request(
            self, organization_id, agent_id, path, *, method="GET", body=None, directory=None
        ):
            if path.endswith("/message") and method == "POST":
                assert body and directory
                self.received.append(body["parts"][0]["text"])
                if partial:
                    self.messages["ses_one"].append(
                        {
                            "info": {
                                "id": body["messageID"],
                                "role": "user",
                                "sessionID": "ses_one",
                            },
                            "parts": [],
                        }
                    )
                raise RuntimeUnavailable("Response lost during native admission")
            return await super().request(
                organization_id, agent_id, path, method=method, body=body, directory=directory
            )

    native = Interrupted()
    first = store.enqueue(
        org, agent, "ses_one", Submission(id=uuid4(), text="Unknown admission"), author
    )
    second = store.enqueue(org, agent, "ses_one", Submission(id=uuid4(), text="Queued"), author)

    async def check():
        async with Dispatcher(store, native).run():
            await eventually(lambda: store.get(org, agent, first["id"])["state"] == "uncertain")
        record = store.get(org, agent, first["id"])
        monkeypatch.setattr(
            "fesnyng_backend.host_dispatch.time.time", lambda: record["submitted_at"] + 10
        )
        restored = DispatchStore(HostStore(store.host.settings))
        await Dispatcher(restored, native).step()
        assert restored.get(org, agent, first["id"])["state"] == "unresolved"
        assert restored.get(org, agent, second["id"])["state"] == "queued"
        assert native.received == ["Unknown admission"]

    asyncio.run(check())


def test_native_final_text_does_not_hide_an_unfinished_tool(tmp_path):
    org, agent, store, native, author = interaction_system(tmp_path)
    row = store.enqueue(org, agent, "ses_one", Submission(id=uuid4(), text="Work"), author)
    store.change(row, "submitting", message_id="msg_input")
    native.messages["ses_one"] = [
        {
            "info": {"id": "msg_input", "role": "user", "sessionID": "ses_one"},
            "parts": [{"type": "text", "text": "Work"}],
        },
        {
            "info": {
                "id": "msg_tool",
                "role": "assistant",
                "sessionID": "ses_one",
                "parentID": "msg_input",
            },
            "parts": [{"type": "tool", "state": {"status": "running"}}],
        },
    ]
    native.busy.add("ses_one")
    native.finish("ses_one")
    native.messages["ses_one"][-1]["info"]["parentID"] = "msg_input"
    asyncio.run(Dispatcher(store, native).step())
    assert store.get(org, agent, row["id"])["state"] != "completed"


def test_dispatch_state_transition_detects_a_cancelled_admission(tmp_path):
    org, agent, store, _, author = interaction_system(tmp_path)
    row = store.enqueue(org, agent, "ses_one", Submission(id=uuid4(), text="Work"), author)
    assert store.change(row, "cancelled") is True
    assert store.change(row, "submitting", message_id="msg_lost") is False
    assert store.get(org, agent, row["id"])["native_message_id"] is None


def test_stop_waits_for_native_admission_before_acknowledging_abort(tmp_path):
    org, agent, store, _, author = interaction_system(tmp_path)

    class Delayed(Native):
        def __init__(self):
            super().__init__()
            self.admit = asyncio.Event()
            self.started = asyncio.Event()

        async def request(
            self, organization_id, agent_id, path, *, method="GET", body=None, directory=None
        ):
            if path.endswith("/message") and method == "POST":
                self.started.set()
                await self.admit.wait()
            return await super().request(
                organization_id, agent_id, path, method=method, body=body, directory=directory
            )

    async def check():
        native = Delayed()
        store.enqueue(org, agent, "ses_one", Submission(id=uuid4(), text="Must stop"), author)
        runner = Dispatcher(store, native)
        async with runner.run():
            await native.started.wait()
            stop = store.enqueue(
                org,
                agent,
                "ses_one",
                Submission(id=uuid4(), mode="stop", cancel_queued=True),
                author,
            )
            runner.wake()
            await asyncio.sleep(0.1)
            assert store.get(org, agent, stop["id"])["state"] != "completed"
            assert native.aborts == 1
            native.admit.set()
            await eventually(lambda: store.get(org, agent, stop["id"])["state"] == "completed")
            assert not native.busy
            assert native.aborts == 2

    asyncio.run(check())


def test_steering_waits_for_an_earlier_unvalidated_native_admission(tmp_path):
    org, agent, store, _, author = interaction_system(tmp_path)

    class DelayedFirst(Native):
        def __init__(self):
            super().__init__()
            self.admit = asyncio.Event()
            self.started = asyncio.Event()

        async def request(
            self, organization_id, agent_id, path, *, method="GET", body=None, directory=None
        ):
            if (
                path.endswith("/message")
                and method == "POST"
                and isinstance(body, dict)
                and body["parts"][0]["text"] == "First"
            ):
                self.started.set()
                await self.admit.wait()
            return await super().request(
                organization_id, agent_id, path, method=method, body=body, directory=directory
            )

    async def check():
        native = DelayedFirst()
        first = store.enqueue(org, agent, "ses_one", Submission(id=uuid4(), text="First"), author)
        runner = Dispatcher(store, native)
        async with runner.run():
            await native.started.wait()
            steering = store.enqueue(
                org,
                agent,
                "ses_one",
                Submission(id=uuid4(), text="Steer", mode="steering"),
                author,
            )
            runner.wake()
            await asyncio.sleep(0.05)
            assert native.received == []
            assert store.get(org, agent, steering["id"])["state"] == "queued"
            native.admit.set()
            await eventually(lambda: native.received == ["First", "Steer"])
            assert store.get(org, agent, first["id"])["receipt_validated"]

    asyncio.run(check())


def test_interrupted_tool_effect_keeps_stop_and_queue_unresolved(tmp_path):
    org, agent, store, _, author = interaction_system(tmp_path)
    first = store.enqueue(org, agent, "ses_one", Submission(id=uuid4(), text="Run"), author)

    class InterruptedAbort(Native):
        async def request(
            self, organization_id, agent_id, path, *, method="GET", body=None, directory=None
        ):
            if path.endswith("/abort"):
                self.aborts += 1
                assert directory is not None
                session = directory.rsplit("/", 1)[-1]
                self.busy.remove(session)
                parent = self.messages[session][0]["info"]["id"]
                self.messages[session].append(
                    {
                        "info": {
                            "id": "msg_aborted",
                            "sessionID": session,
                            "parentID": parent,
                            "role": "assistant",
                            "error": {"name": "MessageAbortedError"},
                            "time": {"completed": 1},
                        },
                        "parts": [],
                    }
                )
                return True
            return await super().request(
                organization_id, agent_id, path, method=method, body=body, directory=directory
            )

    native = InterruptedAbort()

    async def check():
        runner = Dispatcher(store, native)
        runner._STOP_SETTLE_SECONDS = 0.1
        async with runner.run():
            await eventually(lambda: native.received == ["Run"])
            native.messages["ses_one"].append(
                {
                    "info": {
                        "id": "msg_tool",
                        "sessionID": "ses_one",
                        "parentID": native.messages["ses_one"][0]["info"]["id"],
                        "role": "assistant",
                    },
                    "parts": [
                        {
                            "type": "tool",
                            "tool": "bash",
                            "state": {
                                "status": "completed",
                                "metadata": {"interrupted": True, "exit": 130},
                            },
                        }
                    ],
                }
            )
            queued = store.enqueue(
                org, agent, "ses_one", Submission(id=uuid4(), text="Later"), author
            )
            stop = store.enqueue(org, agent, "ses_one", Submission(id=uuid4(), mode="stop"), author)
            runner.wake()
            await eventually(lambda: store.get(org, agent, stop["id"])["state"] == "unresolved")
            assert store.get(org, agent, first["id"])["state"] != "completed"
            assert store.get(org, agent, queued["id"])["state"] == "queued"
            assert native.received == ["Run"]

    asyncio.run(check())


def test_native_tool_effect_predicates_preserve_interruption_ambiguity():
    interrupted = [
        {
            "parts": [
                {
                    "type": "tool",
                    "tool": "bash",
                    "state": {
                        "status": "completed",
                        "metadata": {"interrupted": True, "exit": 130},
                    },
                }
            ]
        }
    ]
    no_exit = [
        {
            "parts": [
                {
                    "type": "tool",
                    "tool": "shell",
                    "state": {"status": "completed", "metadata": {"exit": None}},
                }
            ]
        }
    ]
    running = [{"parts": [{"type": "tool", "state": {"status": "running"}}]}]
    unknown = [{"parts": [{"type": "tool", "state": {"status": "unknown"}}]}]

    assert not native_tools_settled(interrupted)
    assert not native_tools_settled(no_exit)
    assert native_tools_running(running)
    assert not native_tools_settled(running)
    assert native_tools_running(unknown)
    assert native_tools_settled([{"parts": [{"type": "text", "text": "Provider failed"}]}])


def test_unknown_native_command_fails_before_native_admission(tmp_path):
    org, agent, store, _, author = interaction_system(tmp_path)

    class Commands(Native):
        async def request(
            self, organization_id, agent_id, path, *, method="GET", body=None, directory=None
        ):
            if path == "/command":
                return [{"name": "known"}]
            return await super().request(
                organization_id, agent_id, path, method=method, body=body, directory=directory
            )

    async def check():
        runner = Dispatcher(store, Commands())
        receipt = store.enqueue(
            org,
            agent,
            "ses_one",
            Submission(id=uuid4(), text="arguments", command="unknown"),
            author,
        )
        async with runner.run():
            await eventually(lambda: store.get(org, agent, receipt["id"])["state"] == "failed")
        failed = store.get(org, agent, receipt["id"])
        assert failed["native_message_id"] is None
        assert failed["outcome"] == {"kind": "native_command_unavailable", "command": "unknown"}

    asyncio.run(check())


def test_restricted_thread_rejects_native_subtask_command_before_admission(tmp_path):
    org, agent, store, _, author = interaction_system(tmp_path)

    class Commands(Native):
        async def request(
            self, organization_id, agent_id, path, *, method="GET", body=None, directory=None
        ):
            if path == "/command":
                return [{"name": "delegate", "subtask": True}]
            if path.endswith("/command"):
                raise AssertionError("Subtask command must not reach native execution")
            return await super().request(
                organization_id, agent_id, path, method=method, body=body, directory=directory
            )

    class RestrictedPolicy:
        async def apply_policy(self, organization_id, agent_id, session_id):
            return None

        def get_policy(self, organization_id, agent_id, session_id):
            return {
                "desired_revision": 1,
                "applied_revision": 1,
                "applied_policy_version": 1,
                "effective_rules": [{"permission": "*", "pattern": "*", "action": "ask"}],
            }

    async def check():
        runner = Dispatcher(store, Commands(), cast(Interactions, RestrictedPolicy()))
        receipt = store.enqueue(
            org,
            agent,
            "ses_one",
            Submission(id=uuid4(), text="delegate this", command="delegate"),
            author,
        )
        async with runner.run():
            await eventually(lambda: store.get(org, agent, receipt["id"])["state"] == "failed")
        failed = store.get(org, agent, receipt["id"])
        assert failed["native_message_id"] is None
        assert failed["outcome"] == {
            "kind": "native_subtask_command_disallowed",
            "command": "delegate",
        }

    asyncio.run(check())


def test_restricted_thread_rejects_an_inferred_native_subtask_command(tmp_path):
    org, agent, store, _, author = interaction_system(tmp_path)

    class Commands(Native):
        async def request(
            self, organization_id, agent_id, path, *, method="GET", body=None, directory=None
        ):
            assert directory == "/workspace/ses_one"
            if path == "/command":
                return [{"name": "delegate", "agent": "delegate-agent"}]
            if path == "/agent":
                return [{"name": "delegate-agent", "mode": "subagent"}]
            if path.endswith("/command"):
                raise AssertionError("Subtask command must not reach native execution")
            return await super().request(
                organization_id, agent_id, path, method=method, body=body, directory=directory
            )

    class RestrictedPolicy:
        async def apply_policy(self, organization_id, agent_id, session_id):
            return None

        def get_policy(self, organization_id, agent_id, session_id):
            return {
                "desired_revision": 1,
                "applied_revision": 1,
                "applied_policy_version": 1,
                "effective_rules": [{"permission": "*", "pattern": "*", "action": "deny"}],
            }

    async def check():
        runner = Dispatcher(store, Commands(), cast(Interactions, RestrictedPolicy()))
        receipt = store.enqueue(
            org,
            agent,
            "ses_one",
            Submission(id=uuid4(), text="delegate this", command="delegate"),
            author,
        )
        async with runner.run():
            await eventually(lambda: store.get(org, agent, receipt["id"])["state"] == "failed")
        assert store.get(org, agent, receipt["id"])["native_message_id"] is None

    asyncio.run(check())


def test_command_agent_lookup_rechecks_thread_policy_before_native_submission(tmp_path):
    org, agent, store, _, author = interaction_system(tmp_path)

    class Policy:
        def __init__(self):
            self.desired_revision = 1

        async def apply_policy(self, organization_id, agent_id, session_id):
            return None

        def get_policy(self, organization_id, agent_id, session_id):
            return {
                "desired_revision": self.desired_revision,
                "applied_revision": 1,
                "applied_policy_version": 1,
                "effective_rules": [{"permission": "*", "pattern": "*", "action": "ask"}],
            }

    policy = Policy()

    class Commands(Native):
        def __init__(self):
            super().__init__()
            self.agent_lookups = 0

        async def request(
            self, organization_id, agent_id, path, *, method="GET", body=None, directory=None
        ):
            if path == "/command":
                return [{"name": "managed", "agent": "primary"}]
            if path == "/agent":
                self.agent_lookups += 1
                if self.agent_lookups == 2:
                    policy.desired_revision = 2
                return [{"name": "primary", "mode": "primary"}]
            if path.endswith("/command"):
                raise AssertionError("Stale policy must block native command submission")
            return await super().request(
                organization_id, agent_id, path, method=method, body=body, directory=directory
            )

    async def check():
        native = Commands()
        runner = Dispatcher(store, native, cast(Interactions, policy))
        receipt = store.enqueue(
            org,
            agent,
            "ses_one",
            Submission(id=uuid4(), text="arguments", command="managed"),
            author,
        )
        async with runner.run():
            await eventually(lambda: native.agent_lookups >= 2)
            assert store.get(org, agent, receipt["id"])["native_message_id"] is None

    asyncio.run(check())


@pytest.mark.parametrize(
    ("template", "arguments"),
    [
        ("!`echo safe`", ""),
        ("$ARGUMENTS", "!`echo interpolated`"),
        ("$1", "!`echo positional`"),
        ("!$ARGUMENTS", "`echo assembled`"),
        ("!$1", "`echo positional assembled`"),
        ("$1$2", "! `echo split`"),
        ("!`$2`", "unused echo"),
        ("!$ARGUMENTS`echo suffix`", "$'"),
        ("!$0", '"`echo zero`"'),
        ("ordinary template", "!`echo appended`"),
    ],
)
def test_restricted_thread_rejects_shell_executing_native_command_templates(
    tmp_path, template, arguments
):
    org, agent, store, _, author = interaction_system(tmp_path)

    class Commands(Native):
        async def request(
            self, organization_id, agent_id, path, *, method="GET", body=None, directory=None
        ):
            if path == "/command":
                return [{"name": "run", "template": template}]
            if path.endswith("/command"):
                raise AssertionError("Shell template must not reach native execution")
            return await super().request(
                organization_id, agent_id, path, method=method, body=body, directory=directory
            )

    class RestrictedPolicy:
        async def apply_policy(self, organization_id, agent_id, session_id):
            return None

        def get_policy(self, organization_id, agent_id, session_id):
            return {
                "desired_revision": 1,
                "applied_revision": 1,
                "applied_policy_version": 1,
                "effective_rules": [{"permission": "*", "pattern": "*", "action": "ask"}],
            }

    async def check():
        runner = Dispatcher(store, Commands(), cast(Interactions, RestrictedPolicy()))
        receipt = store.enqueue(
            org,
            agent,
            "ses_one",
            Submission(id=uuid4(), text=arguments, command="run"),
            author,
        )
        async with runner.run():
            await eventually(lambda: store.get(org, agent, receipt["id"])["state"] == "failed")
        failed = store.get(org, agent, receipt["id"])
        assert failed["native_message_id"] is None
        assert failed["outcome"] == {
            "kind": "native_command_template_disallowed",
            "command": "run",
        }

    asyncio.run(check())


def test_restricted_thread_allows_an_ordinary_managed_native_command(tmp_path):
    org, agent, store, _, author = interaction_system(tmp_path)

    class Commands(Native):
        def __init__(self):
            super().__init__()
            self.commands = []

        async def request(
            self, organization_id, agent_id, path, *, method="GET", body=None, directory=None
        ):
            if path == "/command":
                return [{"name": "managed"}]
            if path.endswith("/command") and method == "POST":
                self.commands.append(body)
                return None
            return await super().request(
                organization_id, agent_id, path, method=method, body=body, directory=directory
            )

    class RestrictedPolicy:
        async def apply_policy(self, organization_id, agent_id, session_id):
            return None

        def get_policy(self, organization_id, agent_id, session_id):
            return {
                "desired_revision": 1,
                "applied_revision": 1,
                "applied_policy_version": 1,
                "effective_rules": [{"permission": "*", "pattern": "*", "action": "deny"}],
            }

    async def check():
        native = Commands()
        runner = Dispatcher(store, native, cast(Interactions, RestrictedPolicy()))
        store.enqueue(
            org,
            agent,
            "ses_one",
            Submission(id=uuid4(), text="managed arguments", command="managed"),
            author,
        )
        async with runner.run():
            await eventually(lambda: len(native.commands) == 1)
        assert native.commands[0]["command"] == "managed"

    asyncio.run(check())


def test_policy_admissions_wait_for_submitted_native_work_but_not_queued_work(tmp_path):
    org, agent, store, _, author = interaction_system(tmp_path)
    runner = Dispatcher(store, Native())
    queued = store.enqueue(org, agent, "ses_one", Submission(id=uuid4(), text="Queued"), author)
    assert runner.policy_admissions_settled(org, agent, "ses_one")

    assert store.change(queued, "submitting", message_id="msg_input")
    assert not runner.policy_admissions_settled(org, agent, "ses_one")
    submitted = store.get(org, agent, queued["id"])
    assert store.change(
        submitted, "completed", validated=True, outcome={"kind": "native_run_completed"}
    )
    assert runner.policy_admissions_settled(org, agent, "ses_one")


def test_operator_resolution_settles_an_unvalidated_receipt_for_new_policy_admission(tmp_path):
    org, agent, store, _, author = interaction_system(tmp_path)
    runner = Dispatcher(store, Native())
    receipt = store.enqueue(org, agent, "ses_one", Submission(id=uuid4(), text="Unknown"), author)
    assert store.change(receipt, "submitting", message_id="msg_unknown")
    submitted = store.get(org, agent, receipt["id"])
    assert store.change(
        submitted,
        "completed",
        outcome={"kind": "operator_resolution", "outcome": "completed"},
    )

    assert runner.policy_admissions_settled(org, agent, "ses_one")
    next_receipt = store.enqueue(
        org, agent, "ses_one", Submission(id=uuid4(), text="After review"), author
    )
    assert next_receipt["state"] == "queued"
