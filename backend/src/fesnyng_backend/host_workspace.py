"""Allowlisted, scoped OpenCode workspace operations for the host façade."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any, Literal, Protocol, runtime_checkable
from uuid import UUID

from pydantic import Field, TypeAdapter, ValidationError, model_validator

from fesnyng_backend.agent_models import Contract, Name, PermissionRule
from fesnyng_backend.host_dispatch import DispatchStore, HostSubmission
from fesnyng_backend.host_history import capture_session_history
from fesnyng_backend.host_interactions import Interactions
from fesnyng_backend.host_models import Actor, NativeID, SessionCreate, WorkspaceExpectation
from fesnyng_backend.host_runtime import RuntimeRouter, RuntimeUnavailable, WorkspaceSafetyChanged
from fesnyng_backend.host_store import HostStore
from fesnyng_backend.host_workspace_lifecycle import WorkspaceLifecycle, workspace_history_snapshot

_native_id = TypeAdapter(NativeID)


class TextPart(Contract):
    type: Literal["text"]
    text: str = Field(min_length=1, max_length=200_000)


class Prompt(Contract):
    parts: list[TextPart] = Field(default_factory=list, max_length=100)
    command: str | None = Field(default=None, max_length=128, pattern=r"^[A-Za-z0-9_/-]+$")
    mode: Literal["queued", "steering"] = "queued"

    @model_validator(mode="after")
    def require_content(self):
        if not self.command and not self.text().strip():
            raise ValueError("Prompt requires text or a workflow command")
        return self

    def text(self) -> str:
        return "".join(part.text for part in self.parts)


class SessionTime(Contract):
    archived: int | None = None


class SessionUpdate(Contract):
    title: Name | None = None
    time: SessionTime | None = None

    def has_change(self) -> bool:
        return self.title is not None or self.time is not None


class Fork(Contract):
    message_id: NativeID | None = Field(default=None, alias="messageID")


class Revert(Contract):
    message_id: NativeID = Field(alias="messageID")


class QuestionReply(Contract):
    operation_id: UUID
    answers: list[list[str]]


class PermissionReply(Contract):
    operation_id: UUID
    reply: Literal["once", "reject"]


def session_create_kwargs(body: SessionCreate | None) -> dict[str, str]:
    """Pass host-resolved workspace preparation fields only when the caller supplied them."""

    if body is None:
        return {}
    values: dict[str, str] = {}
    if body.creation_id is not None:
        values["creation_id"] = str(body.creation_id)
    if body.project_id is not None:
        values["project_id"] = str(body.project_id)
    if body.requested_checkout_branch is not None:
        values["requested_checkout_branch"] = body.requested_checkout_branch
    if body.repository_url is not None:
        values["repository_url"] = body.repository_url
    if body.checkout_branch is not None:
        values["checkout_branch"] = body.checkout_branch
    return values


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

    async def create_session(
        self,
        organization_id: str,
        agent_id: str,
        title: str,
        workspace: str,
        *,
        creation_id: str | None = None,
        project_id: str | None = None,
        requested_checkout_branch: str | None = None,
        repository_url: str | None = None,
        checkout_branch: str | None = None,
    ) -> dict[str, Any]: ...

    async def workspace_path(
        self, organization_id: str, agent_id: str, directory: str, path: str
    ) -> str: ...

    async def workspace_context(
        self, organization_id: str, agent_id: str, directory: str
    ) -> dict[str, dict[str, int | str | None]]: ...

    async def workspace_metadata_many(
        self, organization_id: str, agent_id: str, directory: str, paths: list[str]
    ) -> dict[str, dict[str, Any]]: ...

    def workspace_download(
        self, organization_id: str, agent_id: str, directory: str, path: str
    ) -> Any: ...

    async def fork_workspace(
        self, organization_id: str, agent_id: str, source_directory: str
    ) -> str: ...

    async def request_with_query(
        self,
        organization_id: str,
        agent_id: str,
        path: str,
        query: dict[str, str],
        *,
        directory: str,
    ) -> Any: ...


@runtime_checkable
class WorkspaceLifecycleRuntime(Protocol):
    """Filesystem and native-admission capabilities used only by lifecycle actions."""

    async def workspace_safety(
        self, organization_id: str, agent_id: str, directory: str
    ) -> dict[str, Any]: ...

    async def remove_workspace(
        self,
        organization_id: str,
        agent_id: str,
        directory: str,
        *,
        discard: bool,
        expected_safety_digest: str,
    ) -> None: ...

    async def replace_workspace(
        self,
        organization_id: str,
        agent_id: str,
        directory: str,
        *,
        repository_url: str | None = None,
        creation_id: str | None = None,
        working_branch: str | None = None,
        working_revision: str | None = None,
    ) -> None: ...

    async def workspace_repository_matches(
        self,
        organization_id: str,
        agent_id: str,
        directory: str,
        *,
        repository_url: str,
        creation_id: str,
    ) -> bool: ...

    async def workspace_exists(
        self, organization_id: str, agent_id: str, directory: str
    ) -> bool: ...

    async def assert_codex_thread_quiet(
        self, organization_id: str, agent_id: str, thread_id: str
    ) -> None: ...


class Workspace:
    def __init__(
        self,
        host: HostStore,
        runtime: NativeRuntime,
        dispatches: DispatchStore,
        interactions: Interactions,
    ):
        self.host = host
        self.runtime = runtime
        self.runtime_router = getattr(runtime, "runtime_router", None) or RuntimeRouter(runtime)
        self.dispatches = dispatches
        self.interactions = interactions

    def _runtime_for(self, session: Mapping[str, Any]) -> NativeRuntime:
        return self.runtime_router.for_session(session)

    def _filesystem_runtime(self, session: Mapping[str, Any]) -> NativeRuntime:
        """Docker path/descriptor checks remain the filesystem authority for both harnesses."""
        return (
            self.runtime if session.get("runtime_type") == "codex" else self._runtime_for(session)
        )

    def _lifecycle_runtime(self) -> WorkspaceLifecycleRuntime:
        if not isinstance(self.runtime, WorkspaceLifecycleRuntime):
            raise RuntimeUnavailable("Workspace lifecycle runtime capabilities are unavailable")
        return self.runtime

    def sessions(self, org: str, agent: str, *, archived: bool = False) -> list[dict[str, Any]]:
        sessions = self.host.sessions(org, agent, archived=None if archived else False)
        incoming = self.dispatches.latest_incoming_at(org, agent)
        sessions.sort(
            key=lambda session: (
                -incoming.get(session["session_id"], session["created_at"] * 1000),
                session["session_id"],
            )
        )
        result = []
        for session in sessions:
            time = {"created": session["created_at"] * 1000}
            if session["archived_at"] is not None:
                time["archived"] = session["archived_at"]
            result.append(
                {
                    "id": session["session_id"],
                    "runtime_type": session["runtime_type"],
                    "frozen": session.get("frozen_at") is not None,
                    "frozen_at": session.get("frozen_at"),
                    "title": session["title"],
                    "directory": session["directory"],
                    "time": time,
                }
            )
        return result

    async def create(self, org: str, agent: str, body: SessionCreate | None) -> dict[str, Any]:
        envelope = self.interactions._applied_envelope(org, agent)
        RuntimeRouter.require_supported(envelope.configuration.runtime_type)
        title = body.title if body is not None else "New thread"
        # The native SDK sends `{}` for a normal create.  Pydantic fills its
        # model default in that case, so distinguish omission from an explicit
        # request for the default workspace.
        workspace = (
            body.workspace
            if body is not None and "workspace" in body.model_fields_set
            else envelope.configuration.workspace
        )
        if workspace != envelope.configuration.workspace:
            raise ValueError("Workspace is not assigned to this agent")
        return await self.runtime.create_session(
            org, agent, title, workspace, **session_create_kwargs(body)
        )

    async def get(self, org: str, agent: str, session_id: str) -> dict[str, Any]:
        session = await self._scoped_session(org, agent, session_id)
        if session.get("frozen_at") is not None:
            return self._project_session(session, self._snapshot(org, agent, session)["session"])
        result = await self._runtime_for(session).request(
            org, agent, f"/session/{session_id}", directory=session["directory"]
        )
        return self._project_session(
            session, self._session_receipt(result, session_id, session["directory"])
        )

    async def context(self, org: str, agent: str, session_id: str) -> dict[str, Any]:
        """Return safe workspace context for one mapped native thread."""
        session = await self._scoped_session(org, agent, session_id)
        runtime = self._filesystem_runtime(session)
        try:
            context = await runtime.workspace_context(org, agent, session["directory"])
        except RuntimeUnavailable:
            context = {
                "repository": {"state": "unavailable"},
                "branch": {"state": "unavailable"},
                "changes": {"state": "unavailable"},
            }
        try:
            subagents = {
                "state": "available",
                "count": await self._child_count(org, agent, session),
            }
        except RuntimeUnavailable:
            subagents = {"state": "unavailable"}
        return {
            **context,
            "subagents": subagents,
            # OpenCode PTYs are runtime-wide and do not provide trustworthy session attribution.
            "backgroundProcesses": {"state": "unavailable"},
        }

    async def workspace_inspection(self, org: str, agent: str, session_id: str) -> dict[str, Any]:
        runtime = self._lifecycle_runtime()
        inspection = await WorkspaceLifecycle(self.host, runtime).inspect(org, agent, session_id)
        if inspection.get("state") != "ready":
            return inspection
        if inspection.get("git", {}).get("state") == "unavailable":
            # The filesystem scan did not produce the evidence needed for a
            # cleanup decision. Do not turn that observation-only failure into
            # an additional native activity probe or a lifecycle-state change.
            return inspection
        session = self.host.session(org, agent, session_id)
        if session.get("frozen_at") is not None:
            return inspection
        try:
            await self._require_workspace_provenance(org, agent, session_id, inspection)
            self._require_no_unsettled_dispatch(org, agent, session_id)
            if session["runtime_type"] == "codex":
                await runtime.assert_codex_thread_quiet(org, agent, session_id)
            else:
                family = [
                    candidate
                    for candidate in await self._scoped_sessions(org, agent)
                    if candidate["root_session_id"] == session_id
                ]
                for candidate in family:
                    status = await self.runtime.request(
                        org,
                        agent,
                        "/session/status",
                        directory=candidate["directory"],
                    )
                    state = (
                        status.get(candidate["session_id"]) if isinstance(status, Mapping) else None
                    )
                    # OpenCode's status receipt is sparse: absent means no active
                    # native work for that session. A present non-idle record is
                    # the only positive evidence that cleanup must wait.
                    if not isinstance(status, Mapping) or (
                        state is not None
                        and (not isinstance(state, Mapping) or state.get("type") != "idle")
                    ):
                        raise RuntimeUnavailable("Native thread family is active or unavailable")
        except (RuntimeUnavailable, ValueError) as error:
            cleanup = inspection.get("cleanup")
            if isinstance(cleanup, dict):
                reason = str(error) or "Native thread activity could not be verified"
                cleanup["remove"] = {"available": False, "reason": reason}
                cleanup["discard"] = {"available": False, "reason": reason}
        return inspection

    async def remove_workspace(
        self,
        org: str,
        agent: str,
        session_id: str,
        expected: WorkspaceExpectation,
        *,
        discard: bool,
        author: Actor,
    ) -> dict[str, Any]:
        runtime = self._lifecycle_runtime()
        lifecycle = WorkspaceLifecycle(self.host, runtime)
        async with self.runtime.lock(agent):
            session = self.host.session(org, agent, session_id)
            binding, safety = await lifecycle.require_current(org, agent, session_id, expected)
            await self._require_workspace_provenance(org, agent, session_id, {"git": safety})
            self._require_unshared_directory(org, agent, session_id, binding["directory"])
            if session.get("frozen_at") is None:
                self.host.require_writable(org, agent, session_id)
                self._require_no_unsettled_dispatch(org, agent, session_id)
                if session["runtime_type"] == "codex":
                    await runtime.assert_codex_thread_quiet(org, agent, session_id)
                else:
                    self._require_idle(
                        await self.runtime.request(
                            org, agent, "/session/status", directory=session["directory"]
                        ),
                        session_id,
                    )
            if not discard and safety.get("state") != "safe":
                raise ValueError("Workspace is not clean enough for removal; use explicit discard")
            snapshot = (
                self._snapshot(org, agent, session)
                if session.get("frozen_at") is not None
                else await capture_session_history(self.host, self.runtime, org, agent, session)
            )
            if session.get("frozen_at") is None and session["runtime_type"] == "opencode":
                for child in self._snapshot_sessions(snapshot):
                    self._require_idle(
                        await self.runtime.request(
                            org,
                            agent,
                            "/session/status",
                            directory=child["directory"],
                        ),
                        child["id"],
                    )
            history_digest = hashlib.sha256(
                json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            self.host.begin_workspace_removal(
                org,
                agent,
                binding["workspace_id"],
                binding["generation"],
                author.model_dump_json(),
                snapshot,
                history_digest,
                safety["digest"],
                working_branch=safety.get("branch")
                if isinstance(safety.get("branch"), str)
                else None,
                working_revision=safety.get("revision")
                if isinstance(safety.get("revision"), str)
                else None,
            )
            try:
                await runtime.remove_workspace(
                    org,
                    agent,
                    binding["directory"],
                    discard=discard,
                    expected_safety_digest=safety["digest"],
                )
            except WorkspaceSafetyChanged:
                # DockerRuntime raises this before starting its destructive shell
                # command, so the directory is known intact and can be retried.
                self.host.restore_workspace_ready(org, agent, binding["workspace_id"])
                raise
            except Exception:
                try:
                    current = await runtime.workspace_safety(org, agent, binding["directory"])
                    if current.get("digest") == safety["digest"]:
                        self.host.restore_workspace_ready(org, agent, binding["workspace_id"])
                    else:
                        self.host.mark_workspace_unavailable(org, agent, binding["workspace_id"])
                except (RuntimeUnavailable, ValueError):
                    self.host.mark_workspace_unavailable(org, agent, binding["workspace_id"])
                raise
            self.host.complete_workspace_removal(org, agent, binding["workspace_id"])
            return await self.workspace_inspection(org, agent, session_id)

    async def replace_workspace(
        self,
        org: str,
        agent: str,
        session_id: str,
        expected: WorkspaceExpectation,
        *,
        author: Actor,
    ) -> dict[str, Any]:
        runtime = self._lifecycle_runtime()
        async with self.runtime.lock(agent):
            session = self.host.session(org, agent, session_id)
            if session.get("frozen_at") is not None:
                raise ValueError("Permanently frozen threads cannot replace a workspace")
            binding = self.host.workspace_binding(org, agent, session_id)
            if (
                binding is None
                or binding["workspace_id"] != expected.workspace_id
                or binding["generation"] != expected.generation
                or binding["state"] != "removed"
                or binding.get("safety_digest") != expected.safety_digest
            ):
                raise ValueError("Workspace replacement request is stale or unavailable")
            retained = workspace_history_snapshot(binding, session)
            if retained is None:
                raise RuntimeUnavailable("Retained workspace history receipt is unavailable")
            self.host.begin_workspace_replacement(
                org,
                agent,
                binding["workspace_id"],
                binding["generation"],
                author.model_dump_json(),
            )
            creation: Mapping[str, Any] | None = None
            try:
                creation = (
                    self.host.workspace_creation(org, agent, binding["creation_id"])
                    if binding.get("creation_id")
                    else None
                )
                await runtime.replace_workspace(
                    org,
                    agent,
                    binding["directory"],
                    repository_url=creation["repository_url"] if creation else None,
                    creation_id=binding.get("creation_id"),
                    working_branch=binding.get("working_branch"),
                    working_revision=binding.get("working_revision"),
                )
                restored = await capture_session_history(
                    self.host, self.runtime, org, agent, session
                )
                if _history_semantics(restored) != _history_semantics(retained):
                    raise RuntimeUnavailable(
                        "Native thread history changed during workspace replacement"
                    )
            except Exception:
                try:
                    exists = await runtime.workspace_exists(org, agent, binding["directory"])
                    if not exists:
                        self.host.restore_workspace_removed(org, agent, binding["workspace_id"])
                    elif await self._replacement_workspace_matches(org, agent, binding, creation):
                        restored = await capture_session_history(
                            self.host, self.runtime, org, agent, session
                        )
                        if _history_semantics(restored) != _history_semantics(retained):
                            raise RuntimeUnavailable(
                                "Native thread history changed during workspace replacement"
                            )
                        self.host.complete_workspace_replacement(
                            org, agent, binding["workspace_id"]
                        )
                        return await self.workspace_inspection(org, agent, session_id)
                    else:
                        self.host.mark_workspace_unavailable(org, agent, binding["workspace_id"])
                except (RuntimeUnavailable, ValueError):
                    self.host.mark_workspace_unavailable(org, agent, binding["workspace_id"])
                raise
            self.host.complete_workspace_replacement(org, agent, binding["workspace_id"])
            return await self.workspace_inspection(org, agent, session_id)

    async def messages(self, org: str, agent: str, session_id: str) -> list[dict[str, Any]]:
        session = await self._scoped_session(org, agent, session_id)
        if session.get("frozen_at") is not None:
            if session["runtime_type"] != "opencode":
                raise RuntimeUnavailable("Thread is bound to the Codex harness")
            return self._snapshot(org, agent, session)["history"]
        root_session_id = session["root_session_id"]
        try:
            root_session = self.host.session(org, agent, root_session_id)
            binding = self.host.workspace_binding(org, agent, root_session_id)
        except LookupError:
            root_session = session
            binding = None
        if binding is not None and binding["state"] != "ready":
            captured = workspace_history_snapshot(binding, root_session)
            history = _captured_history(captured, session_id)
            if not isinstance(history, list):
                raise RuntimeUnavailable("Workspace lifecycle history receipt is unavailable")
            return history
        result = await self._runtime_for(session).request(
            org, agent, f"/session/{session_id}/message", directory=session["directory"]
        )
        if not isinstance(result, list) or not all(
            isinstance(message, dict)
            and isinstance(message.get("info"), dict)
            and message["info"].get("sessionID") == session_id
            and isinstance(message.get("parts"), list)
            for message in result
        ):
            raise RuntimeUnavailable("Native session history response is invalid")
        return result

    async def update(
        self, org: str, agent: str, session_id: str, body: SessionUpdate
    ) -> dict[str, Any]:
        async with self.runtime.lock(agent):
            self.host.require_writable(org, agent, session_id)
            session = self.host.session(org, agent, session_id)
            runtime = self._runtime_for(session)
            if not body.has_change():
                raise ValueError("Native session update requires a title or archive state")
            if body.time is not None:
                self._require_no_unsettled_dispatch(org, agent, session_id)
            native_update: dict[str, Any] = {}
            if body.title is not None:
                native_update["title"] = body.title
            if native_update:
                result = await runtime.request(
                    org,
                    agent,
                    f"/session/{session_id}",
                    method="PATCH",
                    body=native_update,
                    directory=session["directory"],
                )
                receipt = self._session_receipt(result, session_id, session["directory"])
                if receipt.get("title") != body.title:
                    raise RuntimeUnavailable("Native session title was not applied")
            else:
                receipt = {
                    "id": session_id,
                    "directory": session["directory"],
                    "title": session["title"],
                }
            if body.title is not None:
                self.host.rename_session(org, agent, session_id, body.title)
            if body.time is not None:
                # Pinned OpenCode accepts a null archive timestamp without
                # unarchiving its native record.  Fesnyng owns archive state only
                # for mapped-thread navigation, so keep this local and project it
                # onto scoped list/GET/event responses.
                self.host.archive_session(org, agent, session_id, body.time.archived)
            return self._project_session(self.host.session(org, agent, session_id), receipt)

    async def delete(self, org: str, agent: str, session_id: str) -> None:
        async with self.runtime.lock(agent):
            self.host.require_writable(org, agent, session_id)
            session = self.host.session(org, agent, session_id)
            runtime = self._runtime_for(session)
            self._require_no_unsettled_dispatch(org, agent, session_id)
            result = await runtime.request(
                org,
                agent,
                f"/session/{session_id}",
                method="DELETE",
                directory=session["directory"],
            )
            if result is not True:
                raise RuntimeUnavailable("Native session deletion has no verified receipt")
            self.host.delete_session(org, agent, session_id)

    def prompt(
        self, org: str, agent: str, session_id: str, operation_id: UUID, body: Prompt, author: Actor
    ) -> dict[str, Any]:
        submission = HostSubmission(
            id=operation_id, text=body.text(), command=body.command, mode=body.mode, author=author
        )
        return self.dispatches.enqueue(org, agent, session_id, submission, author)

    def stop(
        self,
        org: str,
        agent: str,
        session_id: str,
        operation_id: UUID,
        cancel_queued: bool,
        author: Actor,
    ) -> dict[str, Any]:
        submission = HostSubmission(
            id=operation_id, mode="stop", cancel_queued=cancel_queued, author=author
        )
        return self.dispatches.enqueue(org, agent, session_id, submission, author)

    async def fork(
        self, org: str, agent: str, session_id: str, body: Fork, author: Actor
    ) -> dict[str, Any]:
        source = self.host.session(org, agent, session_id)
        runtime = self._runtime_for(source)
        async with runtime.lock(agent):
            self.host.require_writable(org, agent, session_id)
            self._require_no_unsettled_dispatch(org, agent, session_id)
            self._session_receipt(
                await runtime.request(
                    org, agent, f"/session/{session_id}", directory=source["directory"]
                ),
                session_id,
                source["directory"],
            )
            self._require_idle(
                await runtime.request(org, agent, "/session/status", directory=source["directory"]),
                session_id,
            )
            source_history = self._history_receipt(
                await runtime.request(
                    org, agent, f"/session/{session_id}/message", directory=source["directory"]
                ),
                session_id,
            )
            destination = await runtime.fork_workspace(org, agent, source["directory"])
            try:
                result = await runtime.request(
                    org,
                    agent,
                    f"/session/{session_id}/fork",
                    method="POST",
                    body={"messageID": body.message_id} if body.message_id else {},
                    directory=source["directory"],
                )
                if not isinstance(result, Mapping):
                    raise RuntimeUnavailable("Native session fork receipt is invalid")
                child_id = self._native_id(result.get("id"))
                if child_id == session_id:
                    raise RuntimeUnavailable("Native session fork did not create a fresh child")
                try:
                    self.host.session(org, agent, child_id)
                except LookupError:
                    pass
                else:
                    raise RuntimeUnavailable("Native session fork child is already mapped")
                if result.get("directory") != source["directory"]:
                    raise RuntimeUnavailable(
                        "Native session fork receipt escaped its source workspace"
                    )
                if result.get("parentID") is not None and result.get("parentID") != session_id:
                    raise RuntimeUnavailable("Native session fork ancestry is invalid")
                title = result.get("title")
                if not isinstance(title, str) or not title:
                    raise RuntimeUnavailable("Native session fork receipt is invalid")
                child_history_before = self._history_receipt(
                    await runtime.request(
                        org, agent, f"/session/{child_id}/message", directory=source["directory"]
                    ),
                    child_id,
                )
                await runtime.request(
                    org,
                    agent,
                    "/experimental/control-plane/move-session",
                    method="POST",
                    body={
                        "sessionID": child_id,
                        "destination": {"directory": destination},
                        # The managed copy already contains the source's working
                        # tree. Native movement must not transfer or reset it.
                        "moveChanges": False,
                    },
                )
                moved = self._session_receipt(
                    await runtime.request(
                        org, agent, f"/session/{child_id}", directory=destination
                    ),
                    child_id,
                    destination,
                )
                child_history = self._history_receipt(
                    await runtime.request(
                        org, agent, f"/session/{child_id}/message", directory=destination
                    ),
                    child_id,
                )
                source_after = self._history_receipt(
                    await runtime.request(
                        org, agent, f"/session/{session_id}/message", directory=source["directory"]
                    ),
                    session_id,
                )
                self._session_receipt(
                    await runtime.request(
                        org, agent, f"/session/{session_id}", directory=source["directory"]
                    ),
                    session_id,
                    source["directory"],
                )
            except RuntimeUnavailable as error:
                raise RuntimeUnavailable(
                    "Native session fork outcome is uncertain; copied workspace and native child retained for inspection"
                ) from error
            if (
                moved.get("metadata") != result.get("metadata")
                or moved.get("title") != title
                or moved.get("workspaceID") is not None
            ):
                raise RuntimeUnavailable("Native session fork provenance is invalid")
            if source_after != source_history or child_history != child_history_before:
                raise RuntimeUnavailable("Native session fork history provenance is invalid")
            self.host.save_session(org, agent, child_id, destination, title)
            self.host.register_workspace_fork(org, agent, session_id, child_id, destination)
        # Forks copy history but not the native permission suffix.  Materialize
        # the source override on the new session before exposing it.
        source_policy = self.interactions.get_policy(org, agent, session_id)
        rules = [PermissionRule.model_validate(rule) for rule in source_policy["rules"]]
        if rules:
            self.interactions.put_policy(org, agent, child_id, 0, rules, author)
        await self.interactions.apply_policy(org, agent, child_id)
        return dict(moved)

    @staticmethod
    def _require_idle(result: object, session_id: str) -> None:
        if not isinstance(result, Mapping):
            raise RuntimeUnavailable("Native source session is active; fork was not started")
        status = result.get(session_id)
        if status is not None and (not isinstance(status, Mapping) or status.get("type") != "idle"):
            raise RuntimeUnavailable("Native source session is active; fork was not started")

    def _require_unshared_directory(
        self, org: str, agent: str, session_id: str, directory: str
    ) -> None:
        """A managed path may never remove another mapped root or its ancestor."""
        prefix = directory.rstrip("/") + "/"
        for other in self.host.sessions(org, agent, archived=None):
            if other["session_id"] == session_id:
                continue
            candidate = other["directory"].rstrip("/")
            if (
                candidate == directory
                or candidate.startswith(prefix)
                or directory.startswith(candidate + "/")
            ):
                raise RuntimeUnavailable("Workspace directory is shared with another mapped thread")

    async def _require_workspace_provenance(
        self, org: str, agent: str, session_id: str, inspection: Mapping[str, Any]
    ) -> None:
        """Reject a changed Git pointer before lifecycle evidence permits deletion."""
        binding = self.host.workspace_binding(org, agent, session_id)
        if binding is None:
            return
        git = inspection.get("git")
        if not isinstance(git, Mapping) or git.get("kind") != "repository":
            return
        if binding["kind"] == "ordinary":
            raise RuntimeUnavailable(
                "Workspace became a standalone Git repository; retained branches need inspection"
            )
        creation_id = binding.get("creation_id")
        if not isinstance(creation_id, str):
            raise RuntimeUnavailable("Workspace Git repository provenance is unavailable")
        creation = self.host.workspace_creation(org, agent, creation_id)
        repository_url = creation.get("repository_url") if creation else None
        if not isinstance(
            repository_url, str
        ) or not await self._lifecycle_runtime().workspace_repository_matches(
            org,
            agent,
            binding["directory"],
            repository_url=repository_url,
            creation_id=creation_id,
        ):
            raise RuntimeUnavailable(
                "Workspace Git repository provenance changed; cleanup is unavailable"
            )

    async def _replacement_workspace_matches(
        self, org: str, agent: str, binding: Mapping[str, Any], creation: Mapping[str, Any] | None
    ) -> bool:
        """Adopt a replacement only when its exact owned path/ref is independently proven."""
        runtime = self._lifecycle_runtime()
        safety = await runtime.workspace_safety(org, agent, binding["directory"])
        repository_url = creation.get("repository_url") if creation else None
        if isinstance(repository_url, str):
            branch, revision = binding.get("working_branch"), binding.get("working_revision")
            creation_id = binding.get("creation_id")
            return (
                safety.get("kind") == "repository"
                and safety.get("branch") == branch
                and safety.get("revision") == revision
                and isinstance(creation_id, str)
                and await runtime.workspace_repository_matches(
                    org,
                    agent,
                    binding["directory"],
                    repository_url=repository_url,
                    creation_id=creation_id,
                )
            )
        # An ordinary replacement is intentionally an empty managed directory.
        return (
            safety.get("kind") == "ordinary"
            and safety.get("state") == "safe"
            and safety.get("entries") == 0
        )

    @staticmethod
    def _snapshot_sessions(snapshot: Mapping[str, Any]) -> list[dict[str, str]]:
        """Return the exact OpenCode family captured for one destructive operation."""
        found: list[dict[str, str]] = []

        def visit(item: object) -> None:
            if not isinstance(item, Mapping):
                raise RuntimeUnavailable("Native history child receipt is invalid")
            session = item.get("session")
            if not isinstance(session, Mapping):
                raise RuntimeUnavailable("Native history child receipt is invalid")
            session_id, directory = session.get("id"), session.get("directory")
            if not isinstance(session_id, str) or not isinstance(directory, str):
                raise RuntimeUnavailable("Native history child receipt is invalid")
            found.append({"id": session_id, "directory": directory})
            children = item.get("children", {})
            if not isinstance(children, Mapping):
                raise RuntimeUnavailable("Native history child receipt is invalid")
            for child in children.values():
                visit(child)

        visit(snapshot)
        return found

    @staticmethod
    def _history_receipt(result: object, session_id: str) -> list[dict[str, Any]]:
        if not isinstance(result, list) or not all(
            isinstance(message, dict)
            and isinstance(message.get("info"), dict)
            and message["info"].get("sessionID") == session_id
            and isinstance(message.get("parts"), list)
            for message in result
        ):
            raise RuntimeUnavailable("Native session history response is invalid")
        return result

    async def revert(self, org: str, agent: str, session_id: str, body: Revert) -> dict[str, Any]:
        async with self.runtime.lock(agent):
            self.host.require_writable(org, agent, session_id)
            session = self.host.session(org, agent, session_id)
            runtime = self._runtime_for(session)
            self._require_no_unsettled_dispatch(org, agent, session_id)
            try:
                result = await runtime.request(
                    org,
                    agent,
                    f"/session/{session_id}/revert",
                    method="POST",
                    body={"messageID": body.message_id},
                    directory=session["directory"],
                )
            except RuntimeUnavailable as error:
                raise RuntimeUnavailable(
                    "Native revert outcome is uncertain; inspect history before retrying"
                ) from error
            return self._session_receipt(result, session_id, session["directory"])

    async def unrevert(self, org: str, agent: str, session_id: str) -> dict[str, Any]:
        async with self.runtime.lock(agent):
            self.host.require_writable(org, agent, session_id)
            session = self.host.session(org, agent, session_id)
            runtime = self._runtime_for(session)
            self._require_no_unsettled_dispatch(org, agent, session_id)
            result = await runtime.request(
                org,
                agent,
                f"/session/{session_id}/unrevert",
                method="POST",
                body={},
                directory=session["directory"],
            )
            return self._session_receipt(result, session_id, session["directory"])

    async def pending_all(
        self, org: str, agent: str, kind: Literal["question", "permission"]
    ) -> list[dict[str, Any]]:
        pending: list[dict[str, Any]] = []
        for session in await self._scoped_sessions(org, agent):
            for item in await self._pending_native(org, agent, session, kind):
                item = {**item, "rootSessionID": session["root_session_id"]}
                if kind == "permission" and isinstance(item.get("always"), list):
                    item["always"] = []
                pending.append(item)
        return pending

    async def reply_question(
        self, org: str, agent: str, request_id: str, body: QuestionReply, author: Actor
    ):
        existing = self.interactions.existing_reply(
            org, agent, body.operation_id, request_id, "question", body.answers, author
        )
        if existing is not None:
            return self._confirmed_reply(existing)
        session = await self._pending_session(org, agent, request_id, "question")
        receipt = await self.interactions.reply_scoped(
            org,
            agent,
            session["root_session_id"],
            session["session_id"],
            session["directory"],
            body.operation_id,
            request_id,
            "question",
            body.answers,
            author,
        )
        return self._confirmed_reply(receipt)

    async def reject_question(
        self, org: str, agent: str, request_id: str, operation_id: UUID, author: Actor
    ):
        existing = self.interactions.existing_reply(
            org, agent, operation_id, request_id, "question", {"reject": True}, author
        )
        if existing is not None:
            return self._confirmed_reply(existing)
        session = await self._pending_session(org, agent, request_id, "question")
        receipt = await self.interactions.reply_scoped(
            org,
            agent,
            session["root_session_id"],
            session["session_id"],
            session["directory"],
            operation_id,
            request_id,
            "question",
            {"reject": True},
            author,
        )
        return self._confirmed_reply(receipt)

    async def reply_permission(
        self, org: str, agent: str, request_id: str, body: PermissionReply, author: Actor
    ):
        existing = self.interactions.existing_reply(
            org, agent, body.operation_id, request_id, "permission", body.reply, author
        )
        if existing is not None:
            return self._confirmed_reply(existing)
        session = await self._pending_session(org, agent, request_id, "permission")
        receipt = await self.interactions.reply_scoped(
            org,
            agent,
            session["root_session_id"],
            session["session_id"],
            session["directory"],
            body.operation_id,
            request_id,
            "permission",
            body.reply,
            author,
        )
        return self._confirmed_reply(receipt)

    def models(self, org: str, agent: str) -> dict[str, str]:
        """The applied host envelope is the only selectable model authority."""
        envelope = self.interactions._applied_envelope(org, agent)
        return {
            "provider_id": envelope.configuration.provider,
            "model_id": envelope.configuration.model,
        }

    def commands(self, org: str, agent: str) -> list[dict[str, str]]:
        envelope = self.interactions._applied_envelope(org, agent)
        return [
            {"name": f"fesnyng/{skill.name}", "description": skill.name}
            for skill in envelope.configuration.skills
            if skill.explicit_only
        ]

    def config(self, org: str, agent: str) -> dict[str, str]:
        envelope = self.interactions._applied_envelope(org, agent)
        return {
            "model": f"{envelope.configuration.provider}/{envelope.configuration.model}",
            "permission": envelope.policy.default_permission,
        }

    async def files(self, org: str, agent: str, session_id: str, path: str) -> dict[str, Any]:
        path = self._artifact_path(path, allow_root=True)
        session = await self._scoped_session(org, agent, session_id)
        runtime = self._filesystem_runtime(session)
        await runtime.workspace_path(org, agent, session["directory"], path)
        if session.get("runtime_type") == "codex":
            adapter: Any = self._runtime_for(session)
            directory = await runtime.workspace_path(org, agent, session["directory"], path)
            receipt = await adapter.call(org, agent, "fs/readDirectory", {"path": directory})
            entries = receipt.get("entries") if isinstance(receipt, Mapping) else None
            if not isinstance(entries, list) or not all(
                isinstance(item, Mapping)
                and isinstance(item.get("fileName"), str)
                and isinstance(item.get("isDirectory"), bool)
                and isinstance(item.get("isFile"), bool)
                for item in entries
            ):
                raise RuntimeUnavailable("Codex file listing response is invalid")
            native = [{"name": item["fileName"]} for item in entries]
        else:
            native = await runtime.request_with_query(
                org, agent, "/file", {"path": path}, directory=session["directory"]
            )
        if not isinstance(native, list):
            raise RuntimeUnavailable("Native file listing response is invalid")
        candidates: list[tuple[str, str]] = []
        for item in native:
            if not isinstance(item, Mapping) or not isinstance(item.get("name"), str):
                continue
            name = item["name"].rstrip("/")
            if not name or "/" in name or name in {".", ".."}:
                continue
            entry_path = f"{path}/{name}" if path else name
            candidates.append((name, entry_path))
        # Native listings are untrusted. Bound each Docker operation while retaining
        # every entry rather than turning a large repository into N round trips.
        metadata_by_path: dict[str, dict[str, Any]] = {}
        for start in range(0, len(candidates), 512):
            batch = candidates[start : start + 512]
            metadata_by_path.update(
                await runtime.workspace_metadata_many(
                    org, agent, session["directory"], [entry_path for _, entry_path in batch]
                )
            )
        entries: list[dict[str, Any]] = []
        for name, entry_path in candidates:
            metadata = metadata_by_path.get(entry_path)
            if metadata is None or metadata.get("type") not in {"file", "directory"}:
                continue
            size, modified_at = metadata.get("size"), metadata.get("modifiedAt")
            if not isinstance(size, int) or not isinstance(modified_at, int):
                raise RuntimeUnavailable("Workspace file metadata is invalid")
            entries.append(
                {
                    "name": name,
                    "path": entry_path,
                    "type": metadata["type"],
                    "size": size,
                    "modifiedAt": modified_at,
                }
            )
        entries.sort(key=lambda item: (item["type"] != "directory", item["name"].casefold()))
        return {
            "rootSessionID": session["root_session_id"],
            "sessionID": session_id,
            "path": path,
            "entries": entries,
        }

    async def preview(self, org: str, agent: str, session_id: str, path: str) -> dict[str, Any]:
        path = self._artifact_path(path)
        session = await self._scoped_session(org, agent, session_id)
        runtime = self._filesystem_runtime(session)
        async with runtime.workspace_download(org, agent, session["directory"], path) as (
            metadata,
            stream,
        ):
            data = bytearray()
            async for chunk in stream:
                data.extend(chunk)
                if len(data) > 128 * 1024:
                    del data[128 * 1024 :]
                    break
        content = bytes(data)
        try:
            text = content.decode("utf-8")
            binary = "\x00" in text
        except UnicodeDecodeError:
            text, binary = "", True
        content_type = _content_type(content, not binary)
        return {
            "rootSessionID": session["root_session_id"],
            "sessionID": session_id,
            "path": path,
            "type": "binary" if binary else "text",
            "content": base64.b64encode(content).decode() if binary else text,
            "encoding": "base64" if binary else "utf-8",
            "size": metadata["size"],
            "truncated": metadata["size"] > len(content),
            "contentType": content_type,
        }

    @asynccontextmanager
    async def download(
        self, org: str, agent: str, session_id: str, path: str
    ) -> AsyncIterator[tuple[dict[str, Any], AsyncIterator[bytes]]]:
        path = self._artifact_path(path)
        session = await self._scoped_session(org, agent, session_id)
        runtime = self._filesystem_runtime(session)
        async with runtime.workspace_download(org, agent, session["directory"], path) as (
            metadata,
            stream,
        ):
            yield (
                {
                    "rootSessionID": session["root_session_id"],
                    "sessionID": session_id,
                    "path": path,
                    **metadata,
                },
                stream,
            )

    @staticmethod
    def _artifact_path(path: str, *, allow_root: bool = False) -> str:
        normalized = path.rstrip("/")
        if (
            (not normalized and not allow_root)
            or normalized.startswith("/")
            or (normalized and any(part in {"", ".", ".."} for part in normalized.split("/")))
        ):
            raise ValueError("Artifact path must be relative to its mapped workspace")
        return normalized

    async def event_authorized(
        self, org: str, agent: str, session_id: object, directory: str
    ) -> bool:
        try:
            session = await self._scoped_session(org, agent, self._native_id(session_id))
            self._runtime_for(session)
        except (LookupError, RuntimeUnavailable):
            return False
        return session.get("frozen_at") is None and session["directory"] == directory

    def project_event(self, org: str, agent: str, data: object) -> object:
        """Hide a pinned native unarchive bug behind the scoped host archive registry."""
        if not isinstance(data, dict):
            return data
        properties = data.get("properties")
        if not isinstance(properties, dict):
            return data
        copied = {**data, "properties": dict(properties)}
        # Native permission grants can offer `always`, but the scoped facade
        # supports only one-time approval or rejection.  Keep the live event
        # consistent with the filtered pending snapshot, including children.
        if data.get("type") == "permission.asked" and isinstance(properties.get("always"), list):
            copied["properties"]["always"] = []
        session_id = _event_session_id(data)
        if session_id is None:
            return copied
        try:
            session = self.host.session(org, agent, session_id)
        except LookupError:
            return copied
        info = properties.get("info")
        if isinstance(info, dict):
            copied["properties"]["info"] = self._project_session(session, info)
        return copied

    def _require_no_unsettled_dispatch(self, org: str, agent: str, session_id: str) -> None:
        terminal = {"completed", "failed", "contributed", "cancelled"}
        if any(
            row["state"] not in terminal
            for row in self.dispatches.for_thread(org, agent, session_id)
        ):
            raise RuntimeUnavailable(
                "Native session action is waiting for durable delivery reconciliation"
            )

    @staticmethod
    def _confirmed_reply(receipt: dict[str, Any]) -> dict[str, Any]:
        if receipt.get("state") != "completed":
            raise RuntimeUnavailable(
                "Native interaction reply outcome is uncertain; inspect before retrying"
            )
        return receipt

    async def _pending_session(
        self, org: str, agent: str, request_id: str, kind: Literal["question", "permission"]
    ) -> dict[str, Any]:
        matches = [
            session
            for session in await self._scoped_sessions(org, agent)
            if any(
                item.get("id") == request_id and item.get("sessionID") == session["session_id"]
                for item in await self._pending_native(org, agent, session, kind)
            )
        ]
        if len(matches) != 1:
            raise LookupError("Native interaction is not pending for a mapped thread")
        return matches[0]

    async def _pending_native(
        self, org: str, agent: str, session: dict[str, Any], kind: Literal["question", "permission"]
    ) -> list[dict[str, Any]]:
        result = await self._runtime_for(session).request(
            org, agent, f"/{kind}", directory=session["directory"]
        )
        if not isinstance(result, list) or not all(isinstance(item, dict) for item in result):
            raise RuntimeUnavailable("Native pending interactions response is invalid")
        return [item for item in result if item.get("sessionID") == session["session_id"]]

    async def _scoped_sessions(self, org: str, agent: str) -> list[dict[str, Any]]:
        """List mapped roots and descendants proven by their native trees."""
        result: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        seen: set[str] = set()
        mapped: dict[str, dict[str, Any]] = {}
        for root in self.host.sessions(org, agent):
            if root.get("frozen_at") is not None or root["runtime_type"] != "opencode":
                continue
            record = {**root, "root_session_id": root["session_id"]}
            result.append(record)
            pending.append(record)
            seen.add(root["session_id"])
            mapped[root["session_id"]] = record
        while pending:
            parent = pending.pop()
            children = await self._runtime_for(parent).request(
                org,
                agent,
                f"/session/{parent['session_id']}/children",
                directory=parent["directory"],
            )
            if not isinstance(children, list):
                raise RuntimeUnavailable("Native child sessions response is invalid")
            for child in children:
                if not isinstance(child, Mapping):
                    raise RuntimeUnavailable("Native child session receipt is invalid")
                child_id = self._native_id(child.get("id"))
                directory = child.get("directory")
                if child.get("parentID") != parent["session_id"] or not isinstance(directory, str):
                    raise RuntimeUnavailable("Native child session ancestry is invalid")
                if child_id in mapped:
                    if mapped[child_id]["directory"] != directory:
                        raise RuntimeUnavailable("Native child session ancestry is invalid")
                    # Explicitly mapped forks are roots in the façade even
                    # when native reports their parent relationship.
                    continue
                if child_id in seen:
                    raise RuntimeUnavailable("Native child session ancestry is invalid")
                record = {
                    "session_id": child_id,
                    "directory": directory,
                    "root_session_id": parent["root_session_id"],
                    "runtime_type": parent["runtime_type"],
                }
                seen.add(child_id)
                result.append(record)
                pending.append(record)
        return result

    async def _child_count(self, org: str, agent: str, session: dict[str, Any]) -> int:
        """Count only descendants whose native ancestry is verified from this session."""
        runtime = self._runtime_for(session)
        count = 0
        seen = {session["session_id"]}
        pending = [(session["session_id"], session["directory"])]
        while pending:
            parent_id, directory = pending.pop()
            children = await runtime.request(
                org, agent, f"/session/{parent_id}/children", directory=directory
            )
            if not isinstance(children, list):
                raise RuntimeUnavailable("Native child sessions response is invalid")
            for child in children:
                if not isinstance(child, Mapping):
                    raise RuntimeUnavailable("Native child session receipt is invalid")
                child_id = self._native_id(child.get("id"))
                child_directory = child.get("directory")
                if child.get("parentID") != parent_id or not isinstance(child_directory, str):
                    raise RuntimeUnavailable("Native child session ancestry is invalid")
                if child_id in seen:
                    raise RuntimeUnavailable("Native child session ancestry is invalid")
                seen.add(child_id)
                count += 1
                pending.append((child_id, child_directory))
        return count

    async def _scoped_session(self, org: str, agent: str, session_id: str) -> dict[str, Any]:
        """Resolve a root mapping or a child proven by its mapped root's native tree."""
        try:
            root = self.host.session(org, agent, session_id)
            return {**root, "root_session_id": root["session_id"]}
        except LookupError:
            pass
        roots = self.host.sessions(org, agent)
        for root in self.host.sessions(org, agent, archived=None):
            if root.get("frozen_at") is None:
                continue
            snapshot = self.host.frozen_snapshot(org, agent, root["session_id"])
            child = snapshot.get("children", {}).get(session_id) if snapshot else None
            if child is not None:
                return {
                    **root,
                    "session_id": session_id,
                    "directory": child["session"][
                        "cwd" if root["runtime_type"] == "codex" else "directory"
                    ],
                    "title": child["session"].get("title") or root["title"],
                    "root_session_id": root["session_id"],
                }
        # A removed or unavailable workspace may have stopped the native
        # harness. Its verified lifecycle receipt is sufficient to resolve a
        # retained child history without trying the live OpenCode child tree.
        for root in self.host.sessions(org, agent, archived=None):
            try:
                binding = self.host.workspace_binding(org, agent, root["session_id"])
            except LookupError:
                continue
            if binding is None or binding["state"] == "ready":
                continue
            snapshot = workspace_history_snapshot(binding, root)
            child = _captured_child_snapshot(snapshot, session_id)
            if child is None:
                continue
            native = child.get("session")
            if not isinstance(native, Mapping):
                continue
            directory_key = "cwd" if root["runtime_type"] == "codex" else "directory"
            directory = native.get(directory_key)
            if not isinstance(directory, str):
                continue
            return {
                **root,
                "session_id": session_id,
                "directory": directory,
                "title": native.get("title") or root["title"],
                "root_session_id": root["session_id"],
            }
        roots = [
            row
            for row in roots
            if row.get("frozen_at") is None and row["runtime_type"] == "opencode"
        ]
        mapped = {row["session_id"]: row for row in roots}
        pending = [{**row, "root_session_id": row["session_id"]} for row in roots]
        seen = {root["session_id"] for root in pending}
        while pending:
            parent = pending.pop()
            children = await self._runtime_for(parent).request(
                org,
                agent,
                f"/session/{parent['session_id']}/children",
                directory=parent["directory"],
            )
            if not isinstance(children, list):
                raise RuntimeUnavailable("Native child sessions response is invalid")
            for child in children:
                if not isinstance(child, Mapping):
                    raise RuntimeUnavailable("Native child session receipt is invalid")
                child_id = self._native_id(child.get("id"))
                child_directory = child.get("directory")
                if child.get("parentID") != parent["session_id"] or not isinstance(
                    child_directory, str
                ):
                    raise RuntimeUnavailable("Native child session ancestry is invalid")
                if child_id in mapped:
                    if mapped[child_id]["directory"] != child_directory:
                        raise RuntimeUnavailable("Native child session ancestry is invalid")
                    continue
                if child_id in seen:
                    raise RuntimeUnavailable("Native child session ancestry is invalid")
                if child_id == session_id:
                    return {
                        "session_id": child_id,
                        "directory": child_directory,
                        "root_session_id": parent["root_session_id"],
                        "runtime_type": parent["runtime_type"],
                    }
                seen.add(child_id)
                pending.append(
                    {
                        "session_id": child_id,
                        "directory": child_directory,
                        "root_session_id": parent["root_session_id"],
                        "runtime_type": parent["runtime_type"],
                    }
                )
        raise LookupError("Thread not found")

    def _snapshot(self, org: str, agent: str, session: dict[str, Any]) -> dict[str, Any]:
        root_id = session.get("root_session_id", session["session_id"])
        snapshot = self.host.frozen_snapshot(org, agent, root_id)
        if snapshot is None:
            raise RuntimeUnavailable("Frozen history snapshot is unavailable")
        return (
            snapshot
            if root_id == session["session_id"]
            else snapshot["children"][session["session_id"]]
        )

    @staticmethod
    def _native_id(value: object) -> str:
        try:
            return _native_id.validate_python(value)
        except ValidationError:
            raise RuntimeUnavailable("Native session receipt is invalid") from None

    def _session_receipt(self, result: Any, session_id: str, directory: str) -> dict[str, Any]:
        if not isinstance(result, Mapping) or result.get("id") != session_id:
            raise RuntimeUnavailable("Native session receipt is invalid")
        native_directory = result.get("directory")
        if native_directory is not None and native_directory != directory:
            raise RuntimeUnavailable("Native session receipt escaped its mapped workspace")
        return dict(result)

    @staticmethod
    def _project_session(session: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        if result.get("id") == session["session_id"]:
            result = {
                **result,
                "runtime_type": session["runtime_type"],
                "frozen": session.get("frozen_at") is not None,
                "frozen_at": session.get("frozen_at"),
            }
        # A discovered child has no host registry record.  Native child data is
        # read-only and must not inherit its root's archival projection.
        if "archived_at" not in session:
            return result
        projected = dict(result)
        native_time = projected.get("time")
        if not isinstance(native_time, Mapping):
            if session["archived_at"] is None:
                return projected
            native_time = {}
        time = dict(native_time)
        if session.get("archived_at") is None:
            time.pop("archived", None)
        else:
            time["archived"] = session["archived_at"]
        projected["time"] = time
        return projected


def _content_type(data: bytes, text: bool) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return "text/plain; charset=utf-8" if text else "application/octet-stream"


def _event_session_id(data: object) -> str | None:
    if not isinstance(data, dict) or not isinstance(data.get("properties"), dict):
        return None
    properties = data["properties"]
    candidates = [properties.get("sessionID")]
    for field in ("info", "part", "message"):
        value = properties.get(field)
        if isinstance(value, dict):
            candidates.append(value.get("sessionID"))
            if field == "info":
                candidates.append(value.get("id"))
    return next((value for value in candidates if isinstance(value, str)), None)


def _captured_history(snapshot: object, session_id: str) -> object:
    """Find an exact root or child history in a retained lifecycle receipt."""
    if not isinstance(snapshot, Mapping):
        return None
    session = snapshot.get("session")
    if isinstance(session, Mapping) and session.get("id") == session_id:
        return snapshot.get("history")
    children = snapshot.get("children")
    if not isinstance(children, Mapping):
        return None
    for child in children.values():
        found = _captured_history(child, session_id)
        if found is not None:
            return found
    return None


def _captured_child_snapshot(snapshot: object, session_id: str) -> Mapping[str, Any] | None:
    if not isinstance(snapshot, Mapping):
        return None
    children = snapshot.get("children")
    if not isinstance(children, Mapping):
        return None
    for child in children.values():
        if not isinstance(child, Mapping):
            continue
        native = child.get("session")
        if isinstance(native, Mapping) and native.get("id") == session_id:
            return child
        descendant = _captured_child_snapshot(child, session_id)
        if descendant is not None:
            return descendant
    return None


def _history_semantics(
    snapshot: Mapping[str, Any], inherited_runtime_type: str | None = None
) -> dict[str, Any]:
    """Compare retained native history without volatile thread metadata."""
    session = snapshot.get("session")
    if not isinstance(session, Mapping) or not isinstance(session.get("id"), str):
        raise RuntimeUnavailable("Native thread history receipt is invalid")
    runtime_type = snapshot.get("runtime_type", inherited_runtime_type)
    if runtime_type not in {"opencode", "codex"}:
        raise RuntimeUnavailable("Native thread history receipt is invalid")
    directory_key = "cwd" if runtime_type == "codex" else "directory"
    directory = session.get(directory_key)
    if not isinstance(directory, str):
        raise RuntimeUnavailable("Native thread history receipt is invalid")
    history = snapshot.get("history")
    if runtime_type == "codex":
        if not isinstance(history, Mapping) or not isinstance(history.get("turns"), list):
            raise RuntimeUnavailable("Native thread history receipt is invalid")
        content: object = history["turns"]
    elif not isinstance(history, list):
        raise RuntimeUnavailable("Native thread history receipt is invalid")
    else:
        content = history
    children = snapshot.get("children", {})
    if not isinstance(children, Mapping):
        raise RuntimeUnavailable("Native thread history receipt is invalid")
    return {
        "runtime_type": runtime_type,
        "id": session["id"],
        "directory": directory,
        "history": content,
        "children": [
            _history_semantics(child, runtime_type)
            for _child_id, child in sorted(children.items(), key=lambda item: item[0])
        ],
    }
