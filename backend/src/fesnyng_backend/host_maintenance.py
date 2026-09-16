"""Host-local maintenance admission that remains closed until an explicit release."""

import asyncio
from contextlib import AsyncExitStack
from typing import Any

from fesnyng_backend.host_runtime import RuntimeUnavailable
from fesnyng_backend.host_store import HostStore


class MaintenanceAlreadyActive(ValueError):
    """Another maintenance invocation already closed new-work admission."""


class MaintenanceBusy(ValueError):
    """The host is not safely quiet; ordinary deferral reopens admission."""


class MaintenanceGuard:
    def __init__(self, store: HostStore, runtime: Any | None = None):
        self.store = store
        self.runtime = runtime
        self.login_tasks: set[asyncio.Task[object]] | None = None
        self.interactions: Any | None = None
        self.activity_locks: list[asyncio.Lock] = []
        self.dispatcher: Any | None = None
        self.peer_delivery: Any | None = None

    async def acquire(self) -> dict[str, str]:
        if not self.store.close_maintenance_admission():
            raise MaintenanceAlreadyActive("Maintenance admission is already closed")
        try:
            await self.require_quiet()
        except MaintenanceBusy:
            self.store.open_maintenance_admission()
            raise
        return self.store.maintenance_status()

    async def require_quiet(self) -> None:
        """Hold runtime locks while verifying every owner has settled work."""
        if self.login_tasks and any(not task.done() for task in self.login_tasks):
            raise MaintenanceBusy("credential operation")
        self._require_no_pending_work()
        if self.runtime is None:
            raise RuntimeError("Host maintenance runtime is unavailable")
        agents = self.store.maintenance_agents()
        async with AsyncExitStack() as locks:
            for agent in agents:
                await locks.enter_async_context(self.runtime.lock(agent["agent_id"]))
            for lock in self.activity_locks:
                await locks.enter_async_context(lock)
            self._require_no_pending_work()
            try:
                for agent in agents:
                    state = self.store.agent(agent["organization_id"], agent["agent_id"])
                    if state["desired_state"] != "running":
                        container = await self.runtime.inspect(
                            agent["organization_id"], agent["agent_id"]
                        )
                        if container is None:
                            continue
                        try:
                            if not container["state"]["Running"]:
                                continue
                        except (KeyError, TypeError):
                            raise RuntimeUnavailable("Native container status is invalid") from None
                    await self.runtime.assert_quiet(agent["organization_id"], agent["agent_id"])
                    if (
                        self.interactions is not None
                        and await self.interactions.maintenance_pending(
                            agent["organization_id"], agent["agent_id"]
                        )
                    ):
                        raise MaintenanceBusy("pending interaction")
            except MaintenanceBusy:
                raise
            except (RuntimeUnavailable, ValueError):
                raise MaintenanceBusy("native activity is busy or unavailable") from None
            self._require_no_pending_work()

    def _require_no_pending_work(self) -> None:
        if self.login_tasks and any(not task.done() for task in self.login_tasks):
            raise MaintenanceBusy("credential operation")
        if self.dispatcher is not None and any(
            not task.done()
            for task in [*self.dispatcher.tasks.values(), *self.dispatcher.probes.values()]
        ):
            raise MaintenanceBusy("pending delivery")
        if self.peer_delivery is not None and any(
            not task.done()
            for task in [
                *self.peer_delivery.delivery_tasks.values(),
                *self.peer_delivery.result_tasks.values(),
            ]
        ):
            raise MaintenanceBusy("pending peer work")
        reason = self.store.maintenance_pending_reason()
        if reason is not None:
            raise MaintenanceBusy(reason)

    def release(self) -> dict[str, str]:
        self.store.open_maintenance_admission()
        return self.store.maintenance_status()
