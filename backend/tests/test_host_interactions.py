import asyncio
import json
import secrets
from uuid import uuid4

import pytest

from fesnyng_backend.agent_models import OrganizationPolicy, PermissionRule
from fesnyng_backend.host_interactions import Interactions
from fesnyng_backend.host_models import Actor, HostAgentConfiguration
from fesnyng_backend.host_runtime import RuntimeUnavailable
from fesnyng_backend.host_store import HostStore
from fesnyng_backend.settings import ServiceSettings


def test_question_reply_is_durable_idempotent_and_attributed(tmp_path):
    host, organization_id, agent_id, session_id = _host_with_session(tmp_path)
    native = Native(session_id)
    interactions = Interactions(host, native)
    interactions.initialize()
    _mark_thread_policy_applied(interactions, organization_id, agent_id, session_id)
    author = Actor(kind="human", id=uuid4(), name="Owner", session_id=session_id)
    operation = uuid4()

    async def reply():
        return await interactions.reply(
            organization_id,
            agent_id,
            session_id,
            operation,
            "question-1",
            "question",
            [["Yes"], ["Add context"]],
            author,
        )

    receipt = asyncio.run(reply())
    assert receipt["state"] == "completed"
    assert receipt["author"]["id"] == str(author.id)
    assert native.replies == [
        ("/question/question-1/reply", {"answers": [["Yes"], ["Add context"]]})
    ]
    native.questions = []
    restored = Interactions(HostStore(host.settings), native)

    async def retry():
        return await restored.reply(
            organization_id,
            agent_id,
            session_id,
            operation,
            "question-1",
            "question",
            [["Yes"], ["Add context"]],
            author,
        )

    assert asyncio.run(retry()) == receipt
    assert len(native.replies) == 1

    async def conflict():
        await interactions.reply(
            organization_id,
            agent_id,
            session_id,
            operation,
            "question-1",
            "question",
            [["No"]],
            author,
        )

    with pytest.raises(ValueError, match="conflict"):
        asyncio.run(conflict())


def test_question_rejection_is_durable_and_uses_native_reject_endpoint(tmp_path):
    host, organization_id, agent_id, session_id = _host_with_session(tmp_path)
    native = Native(session_id)
    interactions = Interactions(host, native)
    interactions.initialize()
    _mark_thread_policy_applied(interactions, organization_id, agent_id, session_id)
    author = Actor(kind="human", id=uuid4(), name="Owner")

    receipt = asyncio.run(
        interactions.reject_question(
            organization_id, agent_id, session_id, uuid4(), "question-1", author
        )
    )

    assert receipt["state"] == "completed"
    assert native.replies == [("/question/question-1/reject", {})]


def test_lifecycle_application_normalizes_a_legacy_envelope_for_interaction_admission(tmp_path):
    host, organization_id, agent_id, session_id = _host_with_session(tmp_path)
    envelope = HostAgentConfiguration.model_validate_json(
        host.agent(organization_id, agent_id)["desired_envelope"]
    )
    legacy = envelope.model_dump(mode="json")
    del legacy["configuration"]["runtime_type"]
    with host.connect() as connection:
        connection.execute(
            "UPDATE host_agents SET desired_envelope=?,applied_envelope=? WHERE agent_id=?",
            (json.dumps(legacy), json.dumps(legacy), agent_id),
        )

    # Lifecycle reconciliation reuses the semantic envelope and records one
    # canonical desired/applied pair, so a reply is not held forever on raw
    # JSON differences introduced by the new default.
    host.mark_applied(envelope)
    native = Native(session_id)
    interactions = Interactions(host, native)
    interactions.initialize()
    _mark_thread_policy_applied(interactions, organization_id, agent_id, session_id)

    receipt = asyncio.run(
        interactions.reply(
            organization_id,
            agent_id,
            session_id,
            uuid4(),
            "question-1",
            "question",
            [["Yes"]],
            Actor(kind="human", id=uuid4(), name="Owner"),
        )
    )

    agent = host.agent(organization_id, agent_id)
    assert agent["desired_envelope"] == agent["applied_envelope"] == envelope.model_dump_json()
    assert receipt["state"] == "completed"


def test_replies_require_matching_native_thread_and_allow_only_safe_permission_choices(tmp_path):
    host, organization_id, agent_id, session_id = _host_with_session(tmp_path)
    native = Native(session_id)
    interactions = Interactions(host, native)
    interactions.initialize()
    _mark_thread_policy_applied(interactions, organization_id, agent_id, session_id)
    author = Actor(kind="human", id=uuid4(), name="Owner")
    native.questions = [{"id": "question-1", "sessionID": "ses_other"}]

    async def foreign_question():
        await interactions.reply(
            organization_id,
            agent_id,
            session_id,
            uuid4(),
            "question-1",
            "question",
            [["Yes"]],
            author,
        )

    with pytest.raises(ValueError, match="pending for this thread"):
        asyncio.run(foreign_question())
    assert native.replies == []

    native.permissions = [{"id": "permission-1", "sessionID": session_id}]

    async def permission(answer):
        return await interactions.reply(
            organization_id,
            agent_id,
            session_id,
            uuid4(),
            "permission-1",
            "permission",
            answer,
            author,
        )

    assert asyncio.run(permission("once"))["state"] == "completed"
    with pytest.raises(ValueError, match="once or reject"):
        asyncio.run(permission("always"))
    assert native.replies == [("/permission/permission-1/reply", {"reply": "once"})]


def test_pending_interactions_are_filtered_to_the_requested_thread(tmp_path):
    host, organization_id, agent_id, session_id = _host_with_session(tmp_path)
    native = Native(session_id)
    interactions = Interactions(host, native)
    interactions.initialize()
    native.questions = [
        {"id": "question-here", "sessionID": session_id},
        {"id": "question-other", "sessionID": "ses_other"},
    ]

    pending = asyncio.run(interactions.pending(organization_id, agent_id, session_id, "question"))

    assert pending == [{"id": "question-here", "sessionID": session_id}]


def test_startup_marks_submitting_reply_uncertain_without_replaying_it(tmp_path):
    host, organization_id, agent_id, session_id = _host_with_session(tmp_path)
    native = Native(session_id)
    interactions = Interactions(host, native)
    interactions.initialize()
    author = Actor(kind="human", id=uuid4(), name="Owner")
    operation = uuid4()
    interactions._start_reply(
        organization_id,
        agent_id,
        session_id,
        operation,
        "question-1",
        "question",
        [["Continue"]],
        author,
    )

    restarted = Interactions(HostStore(host.settings), native)
    restarted.initialize()
    assert (
        restarted.get_reply(
            organization_id, agent_id, session_id, operation, "question-1", "question"
        )["state"]
        == "submitting"
    )
    restarted.recover_interrupted()

    receipt = asyncio.run(
        restarted.reply(
            organization_id,
            agent_id,
            session_id,
            operation,
            "question-1",
            "question",
            [["Continue"]],
            author,
        )
    )
    assert receipt["state"] == "uncertain"
    assert native.replies == []


def test_reply_receipts_are_scoped_to_their_thread_and_native_request(tmp_path):
    host, organization_id, agent_id, session_id = _host_with_session(tmp_path)
    interactions = Interactions(host, Native(session_id))
    interactions.initialize()
    author = Actor(kind="human", id=uuid4(), name="Owner")
    operation = uuid4()
    expected = interactions._start_reply(
        organization_id,
        agent_id,
        session_id,
        operation,
        "question-1",
        "question",
        [["Continue"]],
        author,
    )

    assert (
        interactions.get_reply(
            organization_id, agent_id, session_id, operation, "question-1", "question"
        )
        == expected
    )
    with pytest.raises(LookupError):
        interactions.get_reply(
            organization_id, agent_id, "ses_other", operation, "question-1", "question"
        )
    with pytest.raises(LookupError):
        interactions.get_reply(
            organization_id, agent_id, session_id, operation, "question-other", "question"
        )


def test_uncertain_native_reply_is_not_replayed(tmp_path):
    host, organization_id, agent_id, session_id = _host_with_session(tmp_path)
    native = UnavailableNative(session_id)
    interactions = Interactions(host, native)
    interactions.initialize()
    _mark_thread_policy_applied(interactions, organization_id, agent_id, session_id)
    author = Actor(kind="human", id=uuid4(), name="Owner")
    operation = uuid4()

    async def reply():
        return await interactions.reply(
            organization_id,
            agent_id,
            session_id,
            operation,
            "question-1",
            "question",
            [["Yes"]],
            author,
        )

    with pytest.raises(RuntimeUnavailable):
        asyncio.run(reply())
    receipt = asyncio.run(reply())
    assert receipt["state"] == "uncertain"
    assert native.attempts == 1


def test_new_reply_waits_for_pending_thread_policy_then_allows_the_applied_policy(tmp_path):
    host, organization_id, agent_id, session_id = _host_with_session(tmp_path)
    native = Native(session_id)
    interactions = Interactions(host, native)
    interactions.initialize()
    author = Actor(kind="human", id=uuid4(), name="Owner")
    interactions.put_policy(
        organization_id,
        agent_id,
        session_id,
        0,
        [PermissionRule(permission="shell", action="deny")],
        author,
    )

    async def reply():
        return await interactions.reply(
            organization_id,
            agent_id,
            session_id,
            uuid4(),
            "question-1",
            "question",
            [["Continue"]],
            author,
        )

    with pytest.raises(RuntimeUnavailable, match="thread policy"):
        asyncio.run(reply())
    assert native.replies == []

    _mark_thread_policy_applied(interactions, organization_id, agent_id, session_id)
    assert asyncio.run(reply())["state"] == "completed"
    assert native.replies == [("/question/question-1/reply", {"answers": [["Continue"]]})]


def test_thread_policy_aborts_active_work_and_applies_verified_complete_suffix(tmp_path):
    host, organization_id, agent_id, session_id = _host_with_session(tmp_path)
    native = PolicyNative(session_id, busy=True)
    interactions = Interactions(host, native)
    interactions.initialize()
    author = Actor(kind="human", id=uuid4(), name="Owner")
    candidate = HostAgentConfiguration(
        host_id=host.instance_id,
        organization_id=organization_id,
        agent_id=agent_id,
        version=2,
        policy_version=2,
        name="Interaction agent",
        policy=OrganizationPolicy(
            default_permission="ask",
            mandatory_permissions=[PermissionRule(permission="shell", action="deny")],
        ),
    )
    desired = interactions.put_policy(
        organization_id,
        agent_id,
        session_id,
        0,
        [PermissionRule(permission="read", pattern="/workspace/*", action="allow")],
        author,
    )
    assert desired["desired_revision"] == 1 and desired["applied_revision"] == 0

    applied = asyncio.run(
        interactions.apply_policy(organization_id, agent_id, session_id, candidate)
    )
    suffix = [
        {"permission": "*", "pattern": "*", "action": "ask"},
        {"permission": "read", "pattern": "/workspace/*", "action": "allow"},
        {"permission": "shell", "pattern": "*", "action": "deny"},
        {"permission": "task", "pattern": "*", "action": "deny"},
    ]
    assert applied["desired_revision"] == applied["applied_revision"] == 1
    assert native.permissions[-len(suffix) :] == suffix
    assert [call[0] for call in native.calls] == [
        f"/session/{session_id}/children",
        "/session/status",
        f"/session/{session_id}/abort",
        "/session/status",
        "/instance/dispose",
        f"/session/{session_id}",
        f"/session/{session_id}",
    ]

    disabled = candidate.model_copy(
        update={
            "version": 3,
            "policy_version": 3,
            "policy": candidate.policy.model_copy(update={"allow_thread_overrides": False}),
        }
    )
    host.stage_agent(disabled)
    host.mark_applied(disabled)
    with pytest.raises(ValueError, match="disabled"):
        interactions.put_policy(
            organization_id,
            agent_id,
            session_id,
            1,
            [PermissionRule(permission="write", action="allow")],
            author,
        )
    ignored = asyncio.run(interactions.apply_policy(organization_id, agent_id, session_id))
    assert ignored["rules"] == []
    assert ignored["effective_rules"] == [
        {"permission": "*", "pattern": "*", "action": "ask"},
        {"permission": "shell", "pattern": "*", "action": "deny"},
        {"permission": "task", "pattern": "*", "action": "deny"},
    ]


def test_policy_verification_failure_retains_pending_revision(tmp_path):
    host, organization_id, agent_id, session_id = _host_with_session(tmp_path)
    native = PolicyNative(session_id, verify=False)
    interactions = Interactions(host, native)
    interactions.initialize()
    author = Actor(kind="human", id=uuid4(), name="Owner")
    interactions.put_policy(
        organization_id,
        agent_id,
        session_id,
        0,
        [PermissionRule(permission="read", action="allow")],
        author,
    )

    with pytest.raises(RuntimeUnavailable, match="not applied"):
        asyncio.run(interactions.apply_policy(organization_id, agent_id, session_id))
    pending = interactions.get_policy(organization_id, agent_id, session_id)
    assert pending["desired_revision"] == 1 and pending["applied_revision"] == 0


def test_thread_policy_disposes_a_quiet_native_instance_before_applying(tmp_path):
    host, organization_id, agent_id, session_id = _host_with_session(tmp_path)
    native = PolicyNative(session_id, busy=False)
    interactions = Interactions(host, native)
    interactions.initialize()
    interactions.put_policy(
        organization_id,
        agent_id,
        session_id,
        0,
        [PermissionRule(permission="read", action="allow")],
        Actor(kind="human", id=uuid4(), name="Owner"),
    )

    asyncio.run(interactions.apply_policy(organization_id, agent_id, session_id))

    assert [call[0] for call in native.calls] == [
        f"/session/{session_id}/children",
        "/session/status",
        "/session/status",
        "/instance/dispose",
        f"/session/{session_id}",
        f"/session/{session_id}",
    ]


def test_thread_policy_rejects_a_malformed_native_status_entry(tmp_path):
    host, organization_id, agent_id, session_id = _host_with_session(tmp_path)

    class MalformedStatusNative(PolicyNative):
        async def request(
            self, organization_id, agent_id, path, *, method="GET", body=None, directory=None
        ):
            if path == "/session/status":
                return {self.session_id: {}}
            return await super().request(
                organization_id, agent_id, path, method=method, body=body, directory=directory
            )

    native = MalformedStatusNative(session_id)
    interactions = Interactions(host, native)
    interactions.initialize()
    interactions.put_policy(
        organization_id,
        agent_id,
        session_id,
        0,
        [PermissionRule(permission="read", action="allow")],
        Actor(kind="human", id=uuid4(), name="Owner"),
    )

    with pytest.raises(RuntimeUnavailable, match="status response is invalid"):
        asyncio.run(interactions.apply_policy(organization_id, agent_id, session_id))

    pending = interactions.get_policy(organization_id, agent_id, session_id)
    assert pending["desired_revision"] == 1 and pending["applied_revision"] == 0
    assert not any(method == "PATCH" for _, method in native.calls)


def test_policy_quiesces_and_replaces_suffix_for_busy_native_descendants(tmp_path):
    host, organization_id, agent_id, session_id = _host_with_session(tmp_path)
    child_id = "ses_child"
    native = DescendantPolicyNative(session_id, child_id)
    interactions = Interactions(host, native)
    interactions.initialize()
    author = Actor(kind="human", id=uuid4(), name="Owner")
    interactions.put_policy(
        organization_id,
        agent_id,
        session_id,
        0,
        [PermissionRule(permission="read", action="allow")],
        author,
    )

    asyncio.run(interactions.apply_policy(organization_id, agent_id, session_id))

    suffix = [
        {"permission": "*", "pattern": "*", "action": "allow"},
        {"permission": "read", "pattern": "*", "action": "allow"},
    ]
    assert native.aborted == [child_id]
    assert native.permissions[session_id][-len(suffix) :] == suffix
    assert native.permissions[child_id][-len(suffix) :] == suffix
    assert native.calls.index((f"/session/{child_id}/abort", "POST")) < native.calls.index(
        (f"/session/{session_id}", "PATCH")
    )
    assert native.calls.index((f"/session/{child_id}/abort", "POST")) < native.calls.index(
        (f"/session/{child_id}", "PATCH")
    )


def test_policy_fails_closed_for_an_unverified_child_scope(tmp_path):
    host, organization_id, agent_id, session_id = _host_with_session(tmp_path)
    native = DescendantPolicyNative(session_id, "ses_child", invalid_parent=True)
    interactions = Interactions(host, native)
    interactions.initialize()
    interactions.put_policy(
        organization_id,
        agent_id,
        session_id,
        0,
        [PermissionRule(permission="read", action="allow")],
        Actor(kind="human", id=uuid4(), name="Owner"),
    )

    with pytest.raises(RuntimeUnavailable, match="ancestry"):
        asyncio.run(interactions.apply_policy(organization_id, agent_id, session_id))
    assert not any(method == "PATCH" for _, method in native.calls)
    assert interactions.get_policy(organization_id, agent_id, session_id)["applied_revision"] == 0


def test_policy_waits_for_dispatcher_admission_after_aborting_children(tmp_path):
    host, organization_id, agent_id, session_id = _host_with_session(tmp_path)
    native = DescendantPolicyNative(session_id, "ses_child")
    interactions = Interactions(host, native)
    interactions.initialize()
    interactions.native_admissions_settled = lambda *_: False
    interactions.put_policy(
        organization_id,
        agent_id,
        session_id,
        0,
        [PermissionRule(permission="read", action="allow")],
        Actor(kind="human", id=uuid4(), name="Owner"),
    )

    with pytest.raises(RuntimeUnavailable, match="admissions"):
        asyncio.run(interactions.apply_policy(organization_id, agent_id, session_id))
    assert native.aborted == ["ses_child"]
    assert not any(method == "PATCH" for _, method in native.calls)


def test_policy_requires_a_positive_abort_acknowledgement(tmp_path):
    host, organization_id, agent_id, session_id = _host_with_session(tmp_path)
    native = PolicyNative(session_id, busy=True, abort_acknowledged=False)
    interactions = Interactions(host, native)
    interactions.initialize()
    interactions.put_policy(
        organization_id,
        agent_id,
        session_id,
        0,
        [PermissionRule(permission="read", action="allow")],
        Actor(kind="human", id=uuid4(), name="Owner"),
    )

    with pytest.raises(RuntimeUnavailable, match="verified receipt"):
        asyncio.run(interactions.apply_policy(organization_id, agent_id, session_id))
    pending = interactions.get_policy(organization_id, agent_id, session_id)
    assert pending["desired_revision"] == 1 and pending["applied_revision"] == 0


def test_policy_reconciliation_contains_native_failures_and_waits_for_global_config(tmp_path):
    host, organization_id, agent_id, session_id = _host_with_session(tmp_path)
    other_session_id = "ses_other"
    host.save_session(organization_id, agent_id, other_session_id, "/workspace/other", "Other")
    native = ReconcileNative({session_id})
    interactions = Interactions(host, native)
    interactions.initialize()
    author = Actor(kind="human", id=uuid4(), name="Owner")
    for thread_id in (session_id, other_session_id):
        interactions.put_policy(
            organization_id,
            agent_id,
            thread_id,
            0,
            [PermissionRule(permission="read", action="allow")],
            author,
        )

    staged = HostAgentConfiguration(
        host_id=host.instance_id,
        organization_id=organization_id,
        agent_id=agent_id,
        version=2,
        policy_version=2,
        name="Interaction agent",
    )
    host.stage_agent(staged)
    assert asyncio.run(interactions.reconcile_once()) == {}

    host.mark_applied(staged)
    reconciled = asyncio.run(interactions.reconcile_once())
    assert reconciled == {
        f"{organization_id}/{agent_id}/{session_id}": "pending",
        f"{organization_id}/{agent_id}/{other_session_id}": "applied",
    }
    assert interactions.get_policy(organization_id, agent_id, session_id)["applied_revision"] == 0
    assert (
        interactions.get_policy(organization_id, agent_id, other_session_id)["applied_revision"]
        == 1
    )

    native.unverified_sessions.clear()
    assert asyncio.run(interactions.reconcile_once()) == {
        f"{organization_id}/{agent_id}/{session_id}": "applied"
    }


def _host_with_session(tmp_path):
    host = HostStore(
        ServiceSettings(
            service="agent-host",
            database_path=tmp_path / "host.sqlite3",
            state_directory=tmp_path / "state",
        )
    )
    host.initialize()
    organization_id, agent_id, session_id = str(uuid4()), str(uuid4()), "ses_memory"
    host.bind_organization(organization_id, secrets.token_urlsafe(32))
    envelope = HostAgentConfiguration(
        host_id=host.instance_id,
        organization_id=organization_id,
        agent_id=agent_id,
        version=1,
        name="Interaction agent",
    )
    host.stage_agent(envelope)
    host.mark_applied(envelope)
    host.save_session(organization_id, agent_id, session_id, "/workspace/thread", "Thread")
    return host, organization_id, agent_id, session_id


def _mark_thread_policy_applied(
    interactions: Interactions, organization_id: str, agent_id: str, session_id: str
) -> None:
    record = interactions._policy_record(organization_id, agent_id, session_id)
    interactions._mark_policy_applied(
        organization_id,
        agent_id,
        session_id,
        record["desired_revision"],
        interactions._applied_envelope(organization_id, agent_id).policy_version,
    )


class Native:
    def __init__(self, session_id):
        self.session_id = session_id
        self.locks = {}
        self.replies = []
        self.questions = [{"id": "question-1", "sessionID": session_id}]
        self.permissions = []

    def lock(self, agent_id):
        return self.locks.setdefault(agent_id, asyncio.Lock())

    async def request(
        self, organization_id, agent_id, path, *, method="GET", body=None, directory=None
    ):
        if path == "/question":
            return self.questions
        if path == "/permission":
            return self.permissions
        if path.startswith(("/question/", "/permission/")):
            self.replies.append((path, body))
            return None
        raise AssertionError(path)


class UnavailableNative(Native):
    def __init__(self, session_id):
        super().__init__(session_id)
        self.attempts = 0

    async def request(
        self, organization_id, agent_id, path, *, method="GET", body=None, directory=None
    ):
        if path.startswith("/question/"):
            self.attempts += 1
            raise RuntimeUnavailable("Native reply outcome unknown")
        return await super().request(
            organization_id, agent_id, path, method=method, body=body, directory=directory
        )


class PolicyNative(Native):
    def __init__(self, session_id, busy=False, verify=True, abort_acknowledged=True):
        super().__init__(session_id)
        self.busy = busy
        self.verify = verify
        self.permissions = [{"permission": "legacy", "pattern": "*", "action": "allow"}]
        self.calls = []
        self.abort_acknowledged = abort_acknowledged

    async def request(
        self, organization_id, agent_id, path, *, method="GET", body=None, directory=None
    ):
        if path == f"/session/{self.session_id}/children":
            self.calls.append((path, body))
            return []
        if path == "/session/status":
            self.calls.append((path, body))
            return {self.session_id: {"type": "busy" if self.busy else "idle"}}
        if path == f"/session/{self.session_id}/abort":
            self.calls.append((path, body))
            self.busy = False
            return self.abort_acknowledged
        if path == "/instance/dispose":
            self.calls.append((path, body))
            return None
        if path == f"/session/{self.session_id}" and method == "PATCH":
            self.calls.append((path, body))
            if not isinstance(body, dict):
                raise AssertionError("Missing policy body")
            self.permissions.extend(body["permission"])
            return None
        if path == f"/session/{self.session_id}":
            self.calls.append((path, body))
            return {"permission": self.permissions if self.verify else []}
        return await super().request(
            organization_id, agent_id, path, method=method, body=body, directory=directory
        )


class ReconcileNative:
    def __init__(self, unverified_sessions):
        self.locks = {}
        self.unverified_sessions = unverified_sessions
        self.permissions = {}

    def lock(self, agent_id):
        return self.locks.setdefault(agent_id, asyncio.Lock())

    async def request(
        self, organization_id, agent_id, path, *, method="GET", body=None, directory=None
    ):
        if path.endswith("/children"):
            return []
        if path == "/session/status":
            return {}
        if path == "/instance/dispose":
            return None
        if path.startswith("/session/"):
            session_id = path.removeprefix("/session/")
            if method == "PATCH":
                if not isinstance(body, dict):
                    raise AssertionError("Missing policy body")
                self.permissions[session_id] = body["permission"]
                return None
            return {
                "permission": []
                if session_id in self.unverified_sessions
                else self.permissions[session_id]
            }
        raise AssertionError(path)


class DescendantPolicyNative:
    def __init__(self, root_id: str, child_id: str, invalid_parent: bool = False):
        self.root_id = root_id
        self.child_id = child_id
        self.invalid_parent = invalid_parent
        self.locks: dict[str, asyncio.Lock] = {}
        self.busy = {root_id: False, child_id: True}
        self.permissions = {
            root_id: [{"permission": "legacy", "pattern": "*", "action": "allow"}],
            child_id: [{"permission": "legacy-child", "pattern": "*", "action": "deny"}],
        }
        self.calls: list[tuple[str, str]] = []
        self.aborted: list[str] = []

    def lock(self, agent_id):
        return self.locks.setdefault(agent_id, asyncio.Lock())

    async def request(
        self, organization_id, agent_id, path, *, method="GET", body=None, directory=None
    ):
        if path == f"/session/{self.root_id}/children":
            self.calls.append((path, method))
            return [
                {
                    "id": self.child_id,
                    "parentID": "ses_wrong" if self.invalid_parent else self.root_id,
                    "directory": "/workspace/child",
                }
            ]
        if path == f"/session/{self.child_id}/children":
            self.calls.append((path, method))
            return []
        if path == "/session/status":
            session_id = self.child_id if directory == "/workspace/child" else self.root_id
            self.calls.append((path, method))
            return {session_id: {"type": "busy" if self.busy[session_id] else "idle"}}
        if path.startswith("/session/") and path.endswith("/abort"):
            session_id = path.removeprefix("/session/").removesuffix("/abort")
            self.calls.append((path, method))
            self.busy[session_id] = False
            self.aborted.append(session_id)
            return True
        if path == "/instance/dispose":
            self.calls.append((path, method))
            return None
        if path.startswith("/session/"):
            session_id = path.removeprefix("/session/")
            self.calls.append((path, method))
            if method == "PATCH":
                assert isinstance(body, dict)
                self.permissions[session_id].extend(body["permission"])
                return None
            return {"permission": self.permissions[session_id]}
        raise AssertionError(path)


def test_restricted_policy_blocks_native_children_that_cannot_inherit_its_limits(tmp_path):
    from fesnyng_backend.host_models import permission_rules

    host, organization_id, agent_id, _ = _host_with_session(tmp_path)
    broad = HostAgentConfiguration.model_validate_json(
        host.agent(organization_id, agent_id)["applied_envelope"]
    )
    assert permission_rules(broad) == [{"permission": "*", "pattern": "*", "action": "allow"}]
    restricted = broad.model_copy(
        update={
            "policy": OrganizationPolicy(
                mandatory_permissions=[PermissionRule(permission="bash", action="ask")]
            )
        }
    )
    assert permission_rules(restricted)[-1] == {
        "permission": "task",
        "pattern": "*",
        "action": "deny",
    }


def test_reply_rechecks_policy_after_native_pending_lookup(tmp_path):
    host, org, agent, session = _host_with_session(tmp_path)
    author = Actor(kind="human", id=uuid4(), name="Owner")

    class Changing(Native):
        async def request(
            self, organization_id, agent_id, path, *, method="GET", body=None, directory=None
        ):
            if path == "/permission":
                interactions.put_policy(
                    org,
                    agent,
                    session,
                    0,
                    [PermissionRule(permission="bash", action="deny")],
                    author,
                )
                return [{"id": "permission-race", "sessionID": session}]
            return await super().request(
                organization_id, agent_id, path, method=method, body=body, directory=directory
            )

    native = Changing(session)
    interactions = Interactions(host, native)
    interactions.initialize()
    interactions.get_policy(org, agent, session)
    interactions._mark_policy_applied(org, agent, session, 0, 1)
    with pytest.raises(RuntimeUnavailable, match="thread policy"):
        asyncio.run(
            interactions.reply(
                org, agent, session, uuid4(), "permission-race", "permission", "once", author
            )
        )
    assert native.replies == []
