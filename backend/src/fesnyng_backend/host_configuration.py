"""Safe desired-to-applied reconciliation for one autonomous agent host."""

from __future__ import annotations

import asyncio
from typing import Protocol

from fesnyng_backend.host_credentials import CredentialStore
from fesnyng_backend.host_dispatch import DispatchStore
from fesnyng_backend.host_interactions import Interactions
from fesnyng_backend.host_models import HostAgentConfiguration
from fesnyng_backend.host_runtime import RuntimeUnavailable
from fesnyng_backend.host_store import HostStore


class NativeRuntime(Protocol):
    def lock(self, agent_id: str) -> asyncio.Lock: ...

    async def assert_quiet(self, organization_id: str, agent_id: str) -> None: ...

    async def configure(self, envelope: HostAgentConfiguration) -> None: ...


class HostConfiguration:
    def __init__(
        self,
        host: HostStore,
        runtime: NativeRuntime,
        credentials: CredentialStore,
        interactions: Interactions,
        dispatch_store: DispatchStore,
    ) -> None:
        self.host = host
        self.runtime = runtime
        self.credentials = credentials
        self.interactions = interactions
        self.dispatch_store = dispatch_store

    async def apply(self, envelope: HostAgentConfiguration) -> dict[str, object]:
        changed = self.host.stage_agent(envelope)
        if not changed:
            return self.host.agent_status(str(envelope.organization_id), str(envelope.agent_id))
        lifecycle = self.host.agent(str(envelope.organization_id), str(envelope.agent_id))
        if lifecycle["desired_state"] == "stopped" or lifecycle["lifecycle_state"] in {
            "transitioning",
            "recovering",
            "recovery_required",
            "failed",
        }:
            self._mark_pending(str(envelope.organization_id), str(envelope.agent_id))
            return self.host.agent_status(str(envelope.organization_id), str(envelope.agent_id))
        try:
            await self._apply_staged(envelope)
        except (PermissionError, RuntimeUnavailable, ValueError):
            self._mark_pending(str(envelope.organization_id), str(envelope.agent_id))
            raise
        return self.host.agent_status(str(envelope.organization_id), str(envelope.agent_id))

    async def reconcile_once(self) -> dict[str, str]:
        with self.host.connect() as connection:
            rows = connection.execute(
                """SELECT desired_envelope FROM host_agents
                WHERE desired_state='running'
                  AND lifecycle_state IN ('running','pending')
                  AND (applied_envelope IS NULL OR desired_envelope != applied_envelope)"""
            ).fetchall()
        reconciled = await asyncio.gather(
            *(
                self._reconcile_agent(
                    HostAgentConfiguration.model_validate_json(row["desired_envelope"])
                )
                for row in rows
            )
        )
        return dict(reconciled)

    async def apply_agent(self, organization_id: str, agent_id: str) -> str:
        current = self.host.agent(organization_id, agent_id)
        envelope = HostAgentConfiguration.model_validate_json(current["desired_envelope"])
        return (await self._reconcile_agent(envelope, lifecycle_operation=True))[1]

    async def _reconcile_agent(
        self, envelope: HostAgentConfiguration, *, lifecycle_operation: bool = False
    ) -> tuple[str, str]:
        organization_id, agent_id = str(envelope.organization_id), str(envelope.agent_id)
        try:
            await self._apply_staged(envelope, lifecycle_operation=lifecycle_operation)
        except (PermissionError, RuntimeUnavailable, ValueError):
            self._mark_pending(organization_id, agent_id)
            return agent_id, "pending"
        return agent_id, "applied"

    async def _apply_staged(
        self, envelope: HostAgentConfiguration, *, lifecycle_operation: bool = False
    ) -> None:
        organization_id, agent_id = str(envelope.organization_id), str(envelope.agent_id)
        current = self.host.agent(organization_id, agent_id)
        previous = (
            HostAgentConfiguration.model_validate_json(current["applied_envelope"])
            if current["applied_envelope"]
            else None
        )
        if previous is None or previous.policy_version != envelope.policy_version:
            for session in self.host.sessions(organization_id, agent_id):
                await self.interactions.apply_policy(
                    organization_id, agent_id, session["session_id"], envelope
                )
        async with self.runtime.lock(agent_id):
            current = self.host.agent(organization_id, agent_id)
            if current["desired_envelope"] != envelope.model_dump_json():
                raise RuntimeUnavailable("Configuration changed during application")
            lifecycle_allowed = (
                current["lifecycle_state"] in {"pending", "recovering"}
                if lifecycle_operation
                else current["desired_state"] == "running"
                and current["lifecycle_state"] in {"running", "pending"}
            )
            if not lifecycle_allowed:
                raise RuntimeUnavailable("Configuration is waiting for the lifecycle transition")
            self._require_safe_delivery_effects(organization_id, agent_id)
            await self.runtime.assert_quiet(organization_id, agent_id)
            profile_id = envelope.configuration.profile_id
            if profile_id is None:
                self.credentials.unassign_agent(organization_id, agent_id)
            else:
                self.credentials.assign_agent(
                    organization_id, agent_id, str(profile_id), current["agent_token"]
                )
            await self.runtime.configure(envelope)
            self.host.mark_applied(envelope)

    def _require_safe_delivery_effects(self, organization_id: str, agent_id: str) -> None:
        unsettled = [
            receipt
            for receipt in self.dispatch_store.pending()
            if receipt["organization_id"] == organization_id
            and receipt["agent_id"] == agent_id
            and receipt["state"] != "queued"
        ]
        if unsettled:
            raise RuntimeUnavailable("Configuration pending: delivery effects need reconciliation")

    def _mark_pending(self, organization_id: str, agent_id: str) -> None:
        self.host.set_runtime_state(
            organization_id,
            agent_id,
            "pending",
            "Configuration pending; host reconciliation required",
        )
