"""Host-owned agent container lifecycle operations and confirmations."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from typing import Any, Literal, Protocol

from fesnyng_backend.agent_models import Contract
from fesnyng_backend.host_models import Actor
from fesnyng_backend.host_runtime import RuntimeUnavailable
from fesnyng_backend.host_store import HostStore


class LifecycleRequest(Contract):
    action: Literal["start", "stop", "restart", "rebuild"]
    confirmed: bool = False
    code: str | None = None


class HostLifecycleRequest(LifecycleRequest):
    author: Actor


class Runtime(Protocol):
    def lock(self, agent_id: str): ...

    async def inspect(self, organization_id: str, agent_id: str) -> dict[str, Any] | None: ...

    async def start(self, organization_id: str, agent_id: str) -> None: ...

    async def stop(self, organization_id: str, agent_id: str) -> None: ...

    async def restart(self, organization_id: str, agent_id: str) -> None: ...

    async def rebuild(self, organization_id: str, agent_id: str) -> None: ...


class Dispatcher(Protocol):
    async def agent_active(self, organization_id: str, agent_id: str) -> bool: ...

    async def agent_activity(self, organization_id: str, agent_id: str) -> str: ...

    async def quiesce_agent(self, organization_id: str, agent_id: str, author: Actor) -> None: ...

    async def reconcile_agent_effects(self, organization_id: str, agent_id: str) -> None: ...

    def agent_effects_settled(self, organization_id: str, agent_id: str) -> bool: ...


class Configuration(Protocol):
    async def apply_agent(self, organization_id: str, agent_id: str) -> str: ...


class AgentLifecycle:
    _CODE_LIFETIME_SECONDS = 300
    _CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

    def __init__(
        self,
        store: HostStore,
        runtime: Runtime,
        dispatcher: Dispatcher,
        configuration: Configuration,
    ) -> None:
        self.store = store
        self.runtime = runtime
        self.dispatcher = dispatcher
        self.configuration = configuration
        self.operation_locks: dict[str, asyncio.Lock] = {}

    def recover_interrupted(self) -> None:
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE host_agents SET lifecycle_state='recovery_required', "
                "error='Host restarted during lifecycle transition; inspect container and receipts' "
                "WHERE lifecycle_state IN ('transitioning','recovering')"
            )

    async def status(self, organization_id: str, agent_id: str) -> dict[str, Any]:
        status = self.store.agent_status(organization_id, agent_id)
        container = await self.runtime.inspect(organization_id, agent_id)
        status["container_state"] = container["state"]["Status"] if container else "missing"
        stored = status["lifecycle_state"]
        if container is None and stored not in {"recovering", "recovery_required", "failed"}:
            status["lifecycle_state"] = "missing"
        elif stored not in {
            "pending",
            "transitioning",
            "recovering",
            "recovery_required",
            "failed",
        }:
            status["lifecycle_state"] = (
                "running" if container and container["state"]["Running"] else "stopped"
            )
        if status["lifecycle_state"] in {"recovery_required", "failed"}:
            status["retry_action"] = "start" if container else "rebuild"
        return status

    async def perform(
        self, organization_id: str, agent_id: str, request: HostLifecycleRequest
    ) -> dict[str, Any]:
        self.store.require_maintenance_open()
        async with self.operation_locks.setdefault(agent_id, asyncio.Lock()):
            return await self._perform(organization_id, agent_id, request)

    async def _perform(
        self, organization_id: str, agent_id: str, request: HostLifecycleRequest
    ) -> dict[str, Any]:
        self.store.agent(organization_id, agent_id)
        if request.action != "start" and not request.confirmed:
            message = f"Confirm {request.action} for this agent."
            if request.action == "rebuild":
                message = (
                    "Confirm rebuild. The writable container layer will be replaced; retained "
                    "home and workspace volumes remain attached."
                )
            return await self._reply(
                organization_id,
                agent_id,
                confirmation_required=True,
                message=message,
            )

        container = await self.runtime.inspect(organization_id, agent_id)
        activity = (
            await self.dispatcher.agent_activity(organization_id, agent_id)
            if request.action != "start" and container and container["state"]["Running"]
            else ""
        )
        active = bool(activity)
        code_verified = False
        if request.code:
            code_verified = self.store.consume_lifecycle_confirmation(
                organization_id,
                agent_id,
                str(request.author.id),
                request.action,
                self._digest(request.code, activity),
                int(time.time()),
            )
            if not code_verified:
                raise ValueError("Lifecycle confirmation code is invalid or expired")
        if active and not code_verified:
            code = "".join(secrets.choice(self._CODE_ALPHABET) for _ in range(6))
            self.store.save_lifecycle_confirmation(
                organization_id,
                agent_id,
                str(request.author.id),
                request.action,
                self._digest(code, activity),
                int(time.time()) + self._CODE_LIFETIME_SECONDS,
            )
            return await self._reply(
                organization_id,
                agent_id,
                confirmation_required=True,
                confirmation_code=code,
                message="Active work must be interrupted. Enter the confirmation code to continue.",
            )

        previous = self.store.agent(organization_id, agent_id)
        previous_desired = previous["desired_state"]
        previous_lifecycle = previous["lifecycle_state"]
        desired = (
            previous_desired
            if request.action == "rebuild"
            else ("stopped" if request.action == "stop" else "running")
        )
        self.store.begin_lifecycle_transition(
            organization_id, agent_id, previous_desired, previous_lifecycle
        )
        try:
            # Wait out any configuration owner that acquired the shared runtime lock
            # before the durable transition gate was installed.
            async with self.runtime.lock(agent_id):
                pass
            container = await self.runtime.inspect(organization_id, agent_id)
            raced_activity = (
                await self.dispatcher.agent_activity(organization_id, agent_id)
                if request.action != "start" and container and container["state"]["Running"]
                else ""
            )
            raced_active = bool(raced_activity)
            if raced_active and (not code_verified or raced_activity != activity):
                self.store.set_lifecycle_state(
                    organization_id,
                    agent_id,
                    desired=previous_desired,
                    state=previous_lifecycle,
                    error=None,
                )
                code = "".join(secrets.choice(self._CODE_ALPHABET) for _ in range(6))
                self.store.save_lifecycle_confirmation(
                    organization_id,
                    agent_id,
                    str(request.author.id),
                    request.action,
                    self._digest(code, raced_activity),
                    int(time.time()) + self._CODE_LIFETIME_SECONDS,
                )
                return await self._reply(
                    organization_id,
                    agent_id,
                    confirmation_required=True,
                    confirmation_code=code,
                    message="Active work started. Enter the confirmation code to interrupt it.",
                )
            if raced_active:
                await self.dispatcher.quiesce_agent(organization_id, agent_id, request.author)

            if request.action == "start":
                self.store.set_lifecycle_state(
                    organization_id, agent_id, desired="running", state="transitioning"
                )
                async with self.runtime.lock(agent_id):
                    await self.runtime.start(organization_id, agent_id)
                await self.dispatcher.reconcile_agent_effects(organization_id, agent_id)
                if not self.dispatcher.agent_effects_settled(organization_id, agent_id):
                    raise RuntimeUnavailable("Agent start needs delivery reconciliation")
                self.store.set_lifecycle_state(
                    organization_id, agent_id, desired="running", state="pending"
                )
                applied = await self.configuration.apply_agent(organization_id, agent_id)
                state = "running" if applied == "applied" else "pending"
                self.store.set_lifecycle_state(organization_id, agent_id, state=state)
            elif request.action in {"stop", "restart"}:
                if not self.dispatcher.agent_effects_settled(organization_id, agent_id):
                    raise RuntimeUnavailable("Agent lifecycle change needs delivery reconciliation")
                if request.action == "stop":
                    self.store.set_lifecycle_state(
                        organization_id, agent_id, desired="stopped", state="transitioning"
                    )
                    async with self.runtime.lock(agent_id):
                        await self.runtime.stop(organization_id, agent_id)
                    self.store.set_lifecycle_state(
                        organization_id, agent_id, desired="stopped", state="stopped"
                    )
                else:
                    self.store.set_lifecycle_state(
                        organization_id, agent_id, desired="running", state="transitioning"
                    )
                    async with self.runtime.lock(agent_id):
                        await self.runtime.restart(organization_id, agent_id)
                    self.store.set_lifecycle_state(
                        organization_id, agent_id, desired="running", state="running"
                    )
            else:
                if container is not None and not self.dispatcher.agent_effects_settled(
                    organization_id, agent_id
                ):
                    raise RuntimeUnavailable("Agent rebuild needs delivery reconciliation")
                self.store.set_lifecycle_state(organization_id, agent_id, state="recovering")
                async with self.runtime.lock(agent_id):
                    await self.runtime.rebuild(organization_id, agent_id)
                await self.dispatcher.reconcile_agent_effects(organization_id, agent_id)
                if not self.dispatcher.agent_effects_settled(organization_id, agent_id):
                    self.store.set_lifecycle_state(
                        organization_id,
                        agent_id,
                        state="recovery_required",
                        error="Retained delivery effects need outcome review",
                    )
                    return await self._reply(
                        organization_id,
                        agent_id,
                        message="Container rebuilt; retained delivery effects need outcome review.",
                    )
                applied = await self.configuration.apply_agent(organization_id, agent_id)
                if applied != "applied":
                    raise RuntimeUnavailable("Rebuilt agent configuration is pending")
                if desired == "stopped":
                    async with self.runtime.lock(agent_id):
                        await self.runtime.stop(organization_id, agent_id)
                self.store.set_lifecycle_state(organization_id, agent_id, state=desired)
        except asyncio.CancelledError:
            self.store.set_lifecycle_state(
                organization_id,
                agent_id,
                state="recovery_required",
                error="Lifecycle operation was interrupted; inspect container and receipts",
            )
            raise
        except RuntimeUnavailable as error:
            recovery = not self.dispatcher.agent_effects_settled(organization_id, agent_id)
            self.store.set_lifecycle_state(
                organization_id,
                agent_id,
                state="recovery_required" if recovery else "failed",
                error=str(error),
            )
            raise
        except Exception:
            recovery = not self.dispatcher.agent_effects_settled(organization_id, agent_id)
            self.store.set_lifecycle_state(
                organization_id,
                agent_id,
                state="recovery_required" if recovery else "failed",
                error="Lifecycle operation failed; inspect host logs",
            )
            raise
        return await self._reply(
            organization_id, agent_id, message=f"Agent {request.action} completed."
        )

    async def _reply(
        self,
        organization_id: str,
        agent_id: str,
        *,
        confirmation_required: bool = False,
        confirmation_code: str | None = None,
        message: str,
    ) -> dict[str, Any]:
        return {
            **(await self.status(organization_id, agent_id)),
            "confirmation_required": confirmation_required,
            **({"confirmation_code": confirmation_code} if confirmation_code else {}),
            "message": message,
        }

    @staticmethod
    def _digest(code: str, activity: str) -> str:
        return hashlib.sha256(f"{code.strip().upper()}\0{activity}".encode()).hexdigest()
