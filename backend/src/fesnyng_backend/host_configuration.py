"""Safe desired-to-applied reconciliation for one autonomous agent host."""

from __future__ import annotations

import asyncio
from typing import Protocol

from fesnyng_backend.codex_runtime import native_policy
from fesnyng_backend.host_credentials import CredentialStore
from fesnyng_backend.host_dispatch import DispatchStore
from fesnyng_backend.host_history import capture_history
from fesnyng_backend.host_interactions import Interactions
from fesnyng_backend.host_models import HostAgentConfiguration
from fesnyng_backend.host_runtime import RuntimeRouter, RuntimeUnavailable
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
        self.host.require_maintenance_open()
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
        except (PermissionError, RuntimeUnavailable, ValueError) as error:
            self._mark_pending(str(envelope.organization_id), str(envelope.agent_id), str(error))
            raise
        return self.host.agent_status(str(envelope.organization_id), str(envelope.agent_id))

    async def reconcile_once(self) -> dict[str, str]:
        if self.host.maintenance_status()["state"] == "closed":
            return {}
        with self.host.connect() as connection:
            rows = connection.execute(
                """SELECT desired_envelope FROM host_agents
                WHERE desired_state='running'
                  AND lifecycle_state IN ('running','pending')
                  AND (lifecycle_state='pending'
                       OR applied_envelope IS NULL
                       OR desired_envelope != applied_envelope)"""
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

    async def switch_harness(
        self, organization_id: str, agent_id: str, expected_version: int, target_runtime: str
    ) -> dict[str, object]:
        """Capture old native provenance, then atomically freeze every mapped root."""
        self.host.require_maintenance_open()
        source = self.host.agent(organization_id, agent_id)
        if not isinstance(source.get("applied_envelope"), str):
            raise RuntimeUnavailable("Harness switch source configuration is not applied")
        source_envelope = HostAgentConfiguration.model_validate_json(source["applied_envelope"])
        if target_runtime == "codex":
            target = source_envelope.model_copy(
                update={
                    "configuration": source_envelope.configuration.model_copy(
                        update={"runtime_type": "codex"}
                    )
                }
            )
            native_policy(target, [])
        self.host.begin_harness_switch(organization_id, agent_id, expected_version, target_runtime)
        try:
            async with self.runtime.lock(agent_id):
                self.host.require_maintenance_open()
                state = self.host.agent(organization_id, agent_id)
                if state["switch_state"] == "frozen":
                    return self.host.agent_status(organization_id, agent_id)
                if state["desired_state"] != "running" or state["lifecycle_state"] != "running":
                    raise RuntimeUnavailable("Harness switching requires a running, stable agent")
                self._require_safe_switch_effects(organization_id, agent_id)
                await self.runtime.assert_quiet(organization_id, agent_id)
                snapshots = await capture_history(
                    self.host, self.runtime, organization_id, agent_id
                )
                self.host.commit_freeze(organization_id, agent_id, snapshots)
        except BaseException:
            self.host.abort_harness_switch(organization_id, agent_id)
            raise
        return self.host.agent_status(organization_id, agent_id)

    async def _reconcile_agent(
        self, envelope: HostAgentConfiguration, *, lifecycle_operation: bool = False
    ) -> tuple[str, str]:
        organization_id, agent_id = str(envelope.organization_id), str(envelope.agent_id)
        try:
            await self._apply_staged(envelope, lifecycle_operation=lifecycle_operation)
        except (PermissionError, RuntimeUnavailable, ValueError) as error:
            self._mark_pending(organization_id, agent_id, str(error))
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
        if (
            previous is not None
            and (previous.configuration.runtime_type != envelope.configuration.runtime_type)
            and not self.host.harness_switch_frozen_for(
                organization_id,
                agent_id,
                previous.version,
                envelope.configuration.runtime_type,
            )
        ):
            raise RuntimeUnavailable("Harness changes require the thread freeze workflow")
        if envelope.configuration.runtime_type == "codex":
            if not hasattr(self.runtime, "codex"):
                raise RuntimeUnavailable("Codex harness is not available on this host")
        else:
            RuntimeRouter.require_supported(envelope.configuration.runtime_type)
        policy_changed = previous is None or previous.policy_version != envelope.policy_version
        # Codex owns thread policy in its App Server.  Its connection and
        # credentials must be configured first, but an applied agent record is
        # still withheld until every mapped thread has accepted that policy.
        if policy_changed and envelope.configuration.runtime_type != "codex":
            for session in self.host.sessions(organization_id, agent_id):
                if session["frozen_at"] is not None:
                    continue
                await self.interactions.apply_policy(
                    organization_id, agent_id, session["session_id"], envelope
                )
        async with self.runtime.lock(agent_id):
            self.host.require_maintenance_open()
            current = self.host.agent(organization_id, agent_id)
            if HostAgentConfiguration.model_validate_json(current["desired_envelope"]) != envelope:
                raise RuntimeUnavailable("Configuration changed during application")
            current_applied = (
                HostAgentConfiguration.model_validate_json(current["applied_envelope"])
                if current["applied_envelope"]
                else None
            )
            # A concurrent reconciler may have observed the old harness before
            # waiting for this lock.  The first apply can already have rebuilt
            # the runtime and cleared its freeze receipt.  Never run that
            # replacement again from stale pre-lock state.
            if current_applied == envelope and current["lifecycle_state"] == "running":
                return
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
            if envelope.configuration.runtime_type == "codex":
                codex = getattr(self.runtime, "codex", None)
                validate = getattr(codex, "validate_configuration", None)
                if validate is not None:
                    # Discovery has no agent-side effects. Validate before an
                    # assignment change or harness replacement can persist.
                    await validate(envelope)
            if previous is not None and (
                previous.configuration.runtime_type != envelope.configuration.runtime_type
            ):
                switch = getattr(self.runtime, "switch_harness", None)
                if switch is None:
                    raise RuntimeUnavailable("Host runtime cannot replace the selected harness")
                await switch(organization_id, agent_id)
            profile_id = envelope.configuration.profile_id
            if profile_id is None:
                self.credentials.unassign_agent(organization_id, agent_id)
            else:
                self.credentials.assign_agent(
                    organization_id, agent_id, str(profile_id), current["agent_token"]
                )
            await self.runtime.configure(envelope)
            if policy_changed and envelope.configuration.runtime_type == "codex":
                for session in self.host.sessions(organization_id, agent_id):
                    if session["frozen_at"] is not None:
                        continue
                    await self.interactions.apply_policy_locked(
                        organization_id, agent_id, session["session_id"], envelope
                    )
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

    def _require_safe_switch_effects(self, organization_id: str, agent_id: str) -> None:
        """A freeze captures only a fully settled agent, including peer reservations."""
        if any(
            receipt["organization_id"] == organization_id and receipt["agent_id"] == agent_id
            for receipt in self.dispatch_store.pending()
        ):
            raise RuntimeUnavailable(
                "Harness switching requires queued work to complete or be explicitly cancelled"
            )
        with self.host.connect() as connection:
            tables = {
                row["name"]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            checks: list[tuple[str, tuple[str, ...]]] = []
            if "host_interaction_operations" in tables:
                checks.append(
                    (
                        """SELECT 1 FROM host_interaction_operations
                        WHERE organization_id=? AND agent_id=? AND state!='completed' LIMIT 1""",
                        (organization_id, agent_id),
                    )
                )
            if "peer_inbox" in tables:
                checks.append(
                    (
                        """SELECT 1 FROM peer_inbox
                        WHERE organization_id=? AND target_agent=?
                          AND state IN ('reserved','creating','uncertain') LIMIT 1""",
                        (organization_id, agent_id),
                    )
                )
            if "peer_outbox" in tables:
                checks.append(
                    (
                        """SELECT 1 FROM peer_outbox
                        WHERE organization_id=? AND source_agent=?
                          AND state IN ('pending','uncertain') LIMIT 1""",
                        (organization_id, agent_id),
                    )
                )
            if any(connection.execute(query, params).fetchone() for query, params in checks):
                raise RuntimeUnavailable(
                    "Harness switching needs peer or native-effect reconciliation"
                )

    def _mark_pending(self, organization_id: str, agent_id: str, reason: str | None = None) -> None:
        self.host.set_runtime_state(
            organization_id,
            agent_id,
            "pending",
            reason or "Configuration pending; host reconciliation required",
        )
