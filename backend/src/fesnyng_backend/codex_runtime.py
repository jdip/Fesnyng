"""Codex App Server adapter owned by the Docker native-runtime boundary."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from fesnyng_backend.codex_transport import CodexTransport, CredentialAccess
from fesnyng_backend.host_models import HostAgentConfiguration
from fesnyng_backend.host_runtime import RuntimeUnavailable

if TYPE_CHECKING:
    from fesnyng_backend.host_runtime import DockerRuntime


def native_policy(
    envelope: HostAgentConfiguration, overrides: list[Mapping[str, object]]
) -> dict[str, str]:
    """Return the small policy subset the pinned App Server can enforce exactly."""
    if envelope.policy.mandatory_permissions:
        raise RuntimeUnavailable("Codex does not support mandatory permission rules")
    if overrides:
        raise RuntimeUnavailable("Codex does not support thread overrides")
    if envelope.policy.default_permission != "allow":
        raise RuntimeUnavailable(
            f"Codex cannot enforce a default {envelope.policy.default_permission} policy"
        )
    return {
        "approvalPolicy": "never",
        "approvalsReviewer": "user",
        # Docker owns the actual boundary: it exposes only the agent's named
        # home/workspace volumes and its private MCP endpoint.  App Server's
        # workspace-write default blocks required local MCP networking.
        "sandbox": "danger-full-access",
    }


class CodexRuntime:
    """Expose only App Server operations; Docker remains lifecycle owner."""

    def __init__(self, runtime: DockerRuntime) -> None:
        self.runtime = runtime
        self.transport = CodexTransport(endpoint=self._endpoint)
        self.resumed_connections: dict[tuple[str, str, str], str] = {}
        self.started_policies: dict[tuple[str, str, str], tuple[str, dict[str, Any]]] = {}

    def set_credential_access(self, credential_access: CredentialAccess) -> None:
        self.transport.set_credential_access(credential_access)

    async def call(
        self, organization_id: str, agent_id: str, method: str, params: Mapping[str, object]
    ) -> dict[str, Any]:
        await self.runtime.running_port(organization_id, agent_id)
        thread_id = params.get("threadId")
        if isinstance(thread_id, str) and method not in {"thread/start", "thread/resume"}:
            await self._resume(organization_id, agent_id, thread_id)
        receipt = await self.transport.call(organization_id, agent_id, method, params)
        if method == "thread/start":
            thread = receipt.get("thread")
            if isinstance(thread, Mapping) and isinstance(thread.get("id"), str):
                # A just-created thread is already attached to this connection.
                # Codex cannot resume its rollout until the first turn persists.
                self.resumed_connections[
                    (organization_id, agent_id, thread["id"])
                ] = await self.transport.connection_id(organization_id, agent_id)
        if method == "thread/resume" and isinstance(thread_id, str):
            self.resumed_connections[
                (organization_id, agent_id, thread_id)
            ] = await self.transport.connection_id(organization_id, agent_id)
        return receipt

    async def pending(
        self, organization_id: str, agent_id: str, thread_id: str
    ) -> list[dict[str, Any]]:
        await self._resume(organization_id, agent_id, thread_id)
        return await self.transport.pending(organization_id, agent_id, thread_id)

    async def respond(
        self,
        organization_id: str,
        agent_id: str,
        thread_id: str,
        request_id: str,
        response: Mapping[str, object],
    ) -> dict[str, Any]:
        await self._resume(organization_id, agent_id, thread_id)
        return await self.transport.respond(
            organization_id, agent_id, thread_id, request_id, response
        )

    async def events(self, organization_id: str, agent_id: str) -> AsyncIterator[dict[str, Any]]:
        for session in self.runtime.store.sessions(organization_id, agent_id):
            if session["runtime_type"] == "codex" and session["deleted_at"] is None:
                await self._resume(organization_id, agent_id, session["session_id"])
        async for event in self.transport.events(organization_id, agent_id):
            yield event

    async def configure(self, envelope: HostAgentConfiguration) -> None:
        """Write Fesnyng-owned instructions and skills without creating an execution loop."""
        org, agent = str(envelope.organization_id), str(envelope.agent_id)
        # A persisted Codex home may still hold the prior account.  Never let
        # a changed or revoked Fesnyng assignment reuse that connection.
        await self.transport.close_agent(org, agent)
        self.resumed_connections = {
            key: value for key, value in self.resumed_connections.items() if key[:2] != (org, agent)
        }
        self.started_policies = {
            key: value for key, value in self.started_policies.items() if key[:2] != (org, agent)
        }
        # Reject an organization policy before writing files or retaining any
        # native account session that would make it appear applied.
        native_policy(envelope, [])
        await self.runtime.ensure(org, agent)
        instructions = "Use the working directory in the current native environment as this thread's workspace."
        if envelope.configuration.instructions:
            instructions += f"\n\n{envelope.configuration.instructions}"
        await self.runtime.write_file(org, agent, "/home/agent/.codex/AGENTS.md", instructions)
        await self.runtime.docker(
            "exec", self.runtime.name(agent), "rm", "-rf", "/home/agent/.codex/skills/fesnyng"
        )
        for skill in envelope.configuration.skills:
            root = f"/home/agent/.codex/skills/fesnyng/{skill.name}"
            await self.runtime.write_file(
                org,
                agent,
                f"{root}/SKILL.md",
                f"---\nname: {skill.name}\ndescription: {skill.name}\n---\n{skill.content}",
            )
            if skill.explicit_only:
                await self.runtime.write_file(
                    org,
                    agent,
                    f"{root}/agents/openai.yaml",
                    "policy:\n  allow_implicit_invocation: false\n",
                )
        # Establish the authenticated connection now.  It performs initialize and
        # the externally managed ChatGPT login when the host has an assignment.
        await self.call(org, agent, "account/read", {"refreshToken": False})

    async def create_session(
        self,
        organization_id: str,
        agent_id: str,
        title: str,
        workspace: str,
        *,
        directory: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        del metadata  # App Server has no durable caller metadata field.
        agent = self.runtime.store.agent(organization_id, agent_id)
        if not agent["applied_envelope"]:
            raise RuntimeUnavailable("Agent configuration is not applied")
        envelope = HostAgentConfiguration.model_validate_json(agent["applied_envelope"])
        if envelope.configuration.runtime_type != "codex":
            raise RuntimeUnavailable("Agent is not configured for Codex")
        if workspace != envelope.configuration.workspace:
            raise ValueError("Workspace is not assigned to this agent")
        directory = directory or f"/workspace/{workspace}/threads/{uuid4().hex}"
        if not directory.startswith(f"/workspace/{workspace}/"):
            raise ValueError("Workspace directory is not assigned to this agent")
        await self.runtime.docker("exec", self.runtime.name(agent_id), "mkdir", "-p", directory)
        receipt = await self.call(
            organization_id,
            agent_id,
            "thread/start",
            {
                "cwd": directory,
                "model": envelope.configuration.model,
                **native_policy(envelope, []),
            },
        )
        thread = receipt.get("thread")
        thread_id = thread.get("id") if isinstance(thread, Mapping) else None
        if not isinstance(thread, Mapping) or not isinstance(thread_id, str) or not thread_id:
            raise RuntimeUnavailable("Codex thread creation receipt is invalid")
        _verify_policy(receipt, native_policy(envelope, []))
        if receipt.get("cwd") != directory or thread.get("cwd") != directory:
            raise RuntimeUnavailable("Codex thread workspace receipt is invalid")
        self.started_policies[(organization_id, agent_id, thread_id)] = (
            await self.transport.connection_id(organization_id, agent_id),
            native_policy(envelope, []),
        )
        native_title = thread.get("title") if isinstance(thread, Mapping) else None
        if not isinstance(native_title, str) or not native_title:
            native_title = title
        self.runtime.store.save_session(
            organization_id,
            agent_id,
            thread_id,
            directory,
            native_title,
            runtime_type="codex",
        )
        return {"id": thread_id, "title": native_title, "directory": directory}

    async def apply_policy(
        self,
        organization_id: str,
        agent_id: str,
        thread_id: str,
        envelope: HostAgentConfiguration,
        overrides: list[Mapping[str, object]],
    ) -> None:
        policy = native_policy(envelope, overrides)
        key = organization_id, agent_id, thread_id
        connection_id = await self.transport.connection_id(organization_id, agent_id)
        if self.started_policies.get(key) == (connection_id, policy):
            # thread/start already applied and acknowledged this exact policy.
            # Untouched threads have no rollout to resume yet.
            return
        receipt = await self.call(
            organization_id,
            agent_id,
            "thread/resume",
            {"threadId": thread_id, **policy},
        )
        thread = receipt.get("thread")
        if not isinstance(thread, Mapping) or thread.get("id") != thread_id:
            raise RuntimeUnavailable("Codex thread policy receipt is invalid")
        _verify_policy(receipt, policy)

    async def _resume(self, organization_id: str, agent_id: str, thread_id: str) -> None:
        await self.runtime.running_port(organization_id, agent_id)
        key = organization_id, agent_id, thread_id
        connection_id = await self.transport.connection_id(organization_id, agent_id)
        if self.resumed_connections.get(key) == connection_id:
            return
        await self.transport.call(
            organization_id, agent_id, "thread/resume", {"threadId": thread_id}
        )
        self.resumed_connections[key] = await self.transport.connection_id(
            organization_id, agent_id
        )

    def _endpoint(self, organization_id: str, agent_id: str) -> tuple[str, str, str]:
        port = self.runtime.native_port(organization_id, agent_id)
        agent = self.runtime.store.agent(organization_id, agent_id)
        return f"ws://127.0.0.1:{port}", str(agent["runtime_password"]), str(agent["agent_token"])


def _verify_policy(receipt: Mapping[str, Any], expected: Mapping[str, Any]) -> None:
    sandbox = receipt.get("sandbox")
    if (
        receipt.get("approvalPolicy") != expected["approvalPolicy"]
        or receipt.get("approvalsReviewer") != expected["approvalsReviewer"]
        or not isinstance(sandbox, Mapping)
        or sandbox.get("type") != "dangerFullAccess"
    ):
        raise RuntimeUnavailable("Codex did not acknowledge the required native policy")
