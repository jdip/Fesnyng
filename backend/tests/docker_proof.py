"""Owned-resource teardown support for opt-in Docker proofs."""

import shutil
import signal
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fesnyng_backend.host_runtime import DockerRuntime, RuntimeUnavailable


@contextmanager
def _stop_proof_compute_on_interrupt():
    """Raise once for cleanup, then defer repeated interrupts until the run finishes."""
    previous: dict[int, Any] = {}
    received: list[int] = []

    def interrupt(signum, _frame):
        received.append(signum)
        if len(received) > 1:
            return
        raise KeyboardInterrupt(f"Docker proof interrupted by {signal.Signals(signum).name}")

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, interrupt)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


@contextmanager
def _defer_proof_interrupts():
    """Let the bounded teardown stop exact proof compute before restoring signals."""
    received: list[int] = []
    previous: dict[int, Any] = {}

    def defer(signum, _frame):
        received.append(signum)

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, defer)
        yield received
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


async def _dispose_proof(
    runtime: DockerRuntime,
    organization_id: str,
    agent_id: str,
    state_directory: Path,
    *,
    verified: bool,
    snapshot_image: str | None = None,
) -> bool:
    """Stop this proof's compute and remove only its verified successful fixtures."""
    failures: list[Exception] = []

    async def attempt(operation):
        try:
            return await operation()
        except RuntimeUnavailable as error:
            failures.append(error)
            return None

    container = await attempt(lambda: runtime.inspect(organization_id, agent_id))
    if container is not None and container["state"]["Running"]:
        await attempt(lambda: runtime.stop(organization_id, agent_id))
        container = await attempt(lambda: runtime.inspect(organization_id, agent_id))
        if container is not None and container["state"]["Running"]:
            failures.append(RuntimeError("Docker proof container is still running"))

    if verified and not failures:
        if container is not None:
            await attempt(lambda: runtime.docker("rm", runtime.name(agent_id)))
        await attempt(
            lambda: runtime.docker(
                "volume",
                "rm",
                runtime.name(agent_id) + "-home",
                runtime.name(agent_id) + "-workspace",
            )
        )
        if snapshot_image is not None:
            await attempt(lambda: runtime.docker("image", "rm", snapshot_image))
        if not failures:
            try:
                shutil.rmtree(state_directory)
            except OSError as error:
                failures.append(error)

    if failures:
        for error in failures:
            print(
                f"Docker proof teardown failed ({type(error).__name__}); "
                f"inspect state={state_directory}, container={runtime.name(agent_id)}.",
                file=sys.stderr,
                flush=True,
            )
    elif verified:
        print(
            f"Verified Docker proof fixtures removed: state={state_directory}, "
            f"container={runtime.name(agent_id)}.",
            flush=True,
        )
    else:
        print(
            f"Docker proof compute stopped; diagnostic state retained: state={state_directory}, "
            f"container={runtime.name(agent_id)}.",
            flush=True,
        )
    return not failures
