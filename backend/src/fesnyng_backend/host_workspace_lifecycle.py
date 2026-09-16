"""Host-owned inspection and guarded lifecycle for mapped thread workspaces."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from fesnyng_backend.host_models import WorkspaceExpectation
from fesnyng_backend.host_runtime import RuntimeUnavailable
from fesnyng_backend.host_store import HostStore


class WorkspaceLifecycle:
    """Keep destructive workspace evidence and admission with the host filesystem owner."""

    def __init__(self, host: HostStore, runtime: Any) -> None:
        self.host = host
        self.runtime = runtime

    async def inspect(self, organization_id: str, agent_id: str, session_id: str) -> dict[str, Any]:
        """Return actionable evidence for exactly one mapped root, never a guessed path."""
        session = self.host.session(organization_id, agent_id, session_id)
        binding = self.host.workspace_binding(organization_id, agent_id, session_id)
        result: dict[str, Any] = {
            "state": "legacy",
            "kind": "legacy",
            "directory": session["directory"],
            "frozen": session.get("frozen_at") is not None,
            "repository": {"state": "unavailable"},
            "git": {"state": "unavailable"},
            "history": {"state": "unavailable"},
            "cleanup": _unavailable("Host ownership was not recorded for this workspace"),
        }
        if binding is None:
            return result
        result.update(
            workspace_id=binding["workspace_id"],
            generation=binding["generation"],
            state=binding["state"],
            kind=binding["kind"],
            safety_digest=binding.get("safety_digest"),
        )
        if binding.get("creation_id"):
            creation = self.host.workspace_creation(
                organization_id, agent_id, binding["creation_id"]
            )
            if creation.get("repository_url"):
                result["repository"] = {
                    "state": "available",
                    "url": creation["repository_url"],
                    "checkout_branch": creation["checkout_branch"],
                    "starting_revision": creation["starting_revision"],
                }
            else:
                result["repository"] = {"state": "absent"}
        saved = workspace_history_snapshot(binding, session)
        if saved is not None:
            result["history"] = {"state": "verified"}
        elif result["frozen"] and self.host.frozen_snapshot(organization_id, agent_id, session_id):
            result["history"] = {"state": "verified", "captured_at": session["frozen_at"]}
        if binding["state"] != "ready":
            replaceable = (
                binding["state"] == "removed"
                and not result["frozen"]
                and saved is not None
                and bool(binding.get("safety_digest"))
            )
            reason = (
                "Permanently frozen threads cannot replace a workspace"
                if result["frozen"]
                else "Workspace lifecycle or retained history needs verification"
            )
            result["cleanup"] = _unavailable(reason)
            if replaceable:
                result["cleanup"]["replace"] = {"available": True}
            return result
        try:
            safety = await self.runtime.workspace_safety(
                organization_id, agent_id, binding["directory"]
            )
        except RuntimeUnavailable:
            # A failed live scan does not alter the durable lifecycle binding. The
            # workspace remains usable; only destructive cleanup lacks evidence.
            result["cleanup"] = _unavailable("Workspace safety could not be verified")
            return result
        if not isinstance(safety, Mapping) or not isinstance(safety.get("digest"), str):
            raise RuntimeUnavailable("Workspace safety receipt is invalid")
        result["safety_digest"] = safety["digest"]
        result["git"] = {key: value for key, value in safety.items() if key != "digest"}
        if safety.get("branch"):
            result["repository"]["working_branch"] = safety["branch"]
        clean = safety.get("state") == "safe"
        if safety.get("kind") == "repository" and not safety.get("upstream"):
            # A missing upstream proves neither zero unpushed commits nor publication.
            result["git"].pop("ahead", None)
            reason = "The branch has no verified upstream; publication safety is unknown"
        elif safety.get("kind") == "ordinary":
            reason = "The directory contains retained files or entries"
        else:
            reason = "Workspace contains changes, untracked or ignored files, or unpushed commits"
        result["cleanup"] = {
            "remove": {"available": clean, **({} if clean else {"reason": reason})},
            "discard": {"available": True},
            "replace": {"available": False, "reason": "Remove the workspace first"},
        }
        return result

    async def require_current(
        self,
        organization_id: str,
        agent_id: str,
        session_id: str,
        expectation: WorkspaceExpectation,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Re-read current filesystem evidence under the caller's agent lock before mutation."""
        binding = self.host.workspace_binding(organization_id, agent_id, session_id)
        if (
            binding is None
            or binding["workspace_id"] != expectation.workspace_id
            or binding["generation"] != expectation.generation
            or binding["state"] != "ready"
        ):
            raise ValueError("Workspace lifecycle request is stale or not host-owned")
        safety = await self.runtime.workspace_safety(
            organization_id, agent_id, binding["directory"]
        )
        if safety.get("digest") != expectation.safety_digest:
            raise ValueError("Workspace safety changed; inspect again before cleanup")
        return binding, dict(safety)


def _unavailable(reason: str) -> dict[str, dict[str, object]]:
    return {
        "remove": {"available": False, "reason": reason},
        "discard": {"available": False, "reason": reason},
        "replace": {"available": False, "reason": reason},
    }


def workspace_history_snapshot(
    binding: Mapping[str, Any], session: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Validate the retained receipt before presenting it as readable history."""
    try:
        snapshot = json.loads(binding["history_snapshot"])
    except (KeyError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(snapshot, dict):
        return None
    digest = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    native = snapshot.get("session")
    directory_key = "cwd" if session["runtime_type"] == "codex" else "directory"
    if (
        digest != binding.get("history_digest")
        or snapshot.get("runtime_type") != session["runtime_type"]
        or not isinstance(native, dict)
        or native.get("id") != session["session_id"]
        or native.get(directory_key) != session["directory"]
        or "history" not in snapshot
    ):
        return None
    return snapshot
