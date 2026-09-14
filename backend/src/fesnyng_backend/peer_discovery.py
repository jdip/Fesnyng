"""Organization-scoped thread discovery through cached direct peer connections."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any, Protocol
from uuid import UUID

import httpx
from pydantic import Field

from fesnyng_backend.agent_models import Contract, Name, Slug
from fesnyng_backend.codex_history import full_turns, is_unmaterialized
from fesnyng_backend.host_models import NativeID
from fesnyng_backend.host_runtime import RuntimeUnavailable
from fesnyng_backend.host_store import HostStore
from fesnyng_backend.peer_configuration import PeerConfigurationStore


class DiscoveryQuery(Contract):
    agent_id: UUID | None = None
    workspace: Slug | None = None
    topic: str = Field(default="", max_length=200)
    active: bool | None = None


class PeerThread(Contract):
    session_id: NativeID
    organization_id: UUID
    agent_id: UUID
    host_id: UUID
    directory: str
    title: Name
    created_at: int
    workspace: Slug
    active: bool


class UnavailablePeer(Contract):
    host_id: UUID
    agent_id: UUID | None = None
    reason: str


class DiscoveryResult(Contract):
    threads: list[PeerThread]
    unavailable: list[UnavailablePeer]


class NativeReader(Protocol):
    async def request(
        self, organization_id: str, agent_id: str, path: str, *, directory: str | None = None
    ) -> Any: ...


class PeerDiscovery:
    def __init__(
        self,
        host: HostStore,
        config: PeerConfigurationStore,
        runtime: NativeReader,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.host, self.config, self.runtime, self.transport = host, config, runtime, transport

    async def local(self, org: str, query: DiscoveryQuery) -> dict[str, Any]:
        roster = self.config.get(org)["agents"]
        threads: list[dict[str, Any]] = []
        unavailable: list[dict[str, str]] = []

        async def inspect(agent: dict[str, Any]):
            aid = agent["agent_id"]
            if agent["host_id"] != str(self.host.instance_id) or (
                query.agent_id and aid != str(query.agent_id)
            ):
                return
            try:
                sessions = self.host.sessions(org, aid)
                for session in sessions:
                    workspace = session["directory"].split("/")[2]
                    if query.workspace and workspace != query.workspace:
                        continue
                    active = await self._active(org, aid, session)
                    if query.active is not None and active != query.active:
                        continue
                    if query.topic and query.topic.casefold() not in session["title"].casefold():
                        history = await self.read_local(org, aid, session["session_id"])
                        if not any(
                            query.topic.casefold() in part["text"].casefold()
                            for message in history
                            for part in message.get("parts", [])
                            if part.get("type") == "text"
                        ):
                            continue
                    threads.append(
                        PeerThread(
                            session_id=session["session_id"],
                            organization_id=session["organization_id"],
                            agent_id=session["agent_id"],
                            host_id=self.host.instance_id,
                            directory=session["directory"],
                            title=session["title"],
                            created_at=session["created_at"],
                            workspace=workspace,
                            active=active,
                        ).model_dump(mode="json")
                    )
            except (LookupError, RuntimeUnavailable):
                unavailable.append(
                    {
                        "host_id": str(self.host.instance_id),
                        "agent_id": aid,
                        "reason": "Agent threads unavailable",
                    }
                )

        await asyncio.gather(*(inspect(agent) for agent in roster))
        return {"threads": threads, "unavailable": unavailable}

    async def discover(self, org: str, source_agent: str, query: DiscoveryQuery) -> dict[str, Any]:
        self.host.agent(org, source_agent)
        configuration = self.config.get(org)
        roster = configuration["agents"]
        distances = _chart_distances(roster, source_agent)
        agents: list[dict[str, Any]] = [
            {**agent, "chart_distance": distances.get(agent["agent_id"])}
            for agent in roster
            if not query.agent_id or agent["agent_id"] == str(query.agent_id)
        ]
        agents.sort(
            key=lambda agent: (
                agent["chart_distance"] if agent["chart_distance"] is not None else len(roster) + 1,
                agent["name"].casefold(),
                agent["agent_id"],
            )
        )

        async def remote(host_id: str) -> dict[str, Any]:
            try:
                reply = await self._request(
                    org,
                    host_id,
                    "/peer/discovery",
                    {"source_agent": source_agent, "query": query.model_dump(mode="json")},
                )
                result = DiscoveryResult.model_validate(reply)
                for thread in result.threads:
                    agent = self.config.agent(org, str(thread.agent_id))
                    if (
                        str(thread.organization_id) != org
                        or str(thread.host_id) != host_id
                        or agent["host_id"] != host_id
                    ):
                        raise ValueError("Peer discovery identity mismatch")
                if any(str(item.host_id) != host_id for item in result.unavailable):
                    raise ValueError("Peer availability identity mismatch")
                return result.model_dump(mode="json", exclude_none=True)
            except (RuntimeUnavailable, ValueError, LookupError):
                return {
                    "threads": [],
                    "unavailable": [{"host_id": host_id, "reason": "Peer discovery unavailable"}],
                }

        results = await asyncio.gather(
            self.local(org, query), *(remote(peer["host_id"]) for peer in configuration["peers"])
        )
        threads = [row for result in results for row in result["threads"]]
        for thread in threads:
            thread["chart_distance"] = distances.get(thread["agent_id"])
        threads.sort(
            key=lambda row: (
                not row["active"],
                row["chart_distance"] if row["chart_distance"] is not None else len(roster) + 1,
                -row["created_at"],
                row["session_id"],
            )
        )
        return {
            "agents": agents,
            "threads": threads,
            "unavailable": [row for result in results for row in result["unavailable"]],
        }

    async def read_local(self, org: str, agent: str, session_id: str) -> list[dict[str, Any]]:
        session = self.host.session(org, agent, session_id)
        if session["runtime_type"] == "codex":
            adapter = self._codex(session)
            try:
                turns = await full_turns(adapter, org, agent, session_id)
            except RuntimeUnavailable as error:
                if not is_unmaterialized(error, session_id):
                    raise
                return []
            return _codex_peer_history({"data": turns}, session_id)
        history = await self.runtime.request(
            org, agent, f"/session/{session_id}/message", directory=session["directory"]
        )
        return _validated_history(history, session_id)

    def _codex(self, session: dict[str, Any]) -> Any:
        router = getattr(self.runtime, "runtime_router", None)
        adapter = (
            router.for_session(session)
            if router is not None
            else getattr(self.runtime, "codex", None)
        )
        if adapter is None or not hasattr(adapter, "call"):
            raise RuntimeUnavailable("Codex harness is not available on this host")
        return adapter

    async def _active(self, org: str, agent: str, session: dict[str, Any]) -> bool:
        if session["runtime_type"] == "codex":
            try:
                turns = await full_turns(self._codex(session), org, agent, session["session_id"])
            except RuntimeUnavailable as error:
                if not is_unmaterialized(error, session["session_id"]):
                    raise
                return False
            return any(
                turn.get("status") not in {"completed", "failed", "interrupted"} for turn in turns
            )
        statuses = await self.runtime.request(
            org, agent, "/session/status", directory=session["directory"]
        )
        if not isinstance(statuses, dict):
            raise RuntimeUnavailable("Invalid native activity")
        activity = statuses.get(session["session_id"], {"type": "idle"})
        if not isinstance(activity, dict) or activity.get("type") not in ("idle", "busy", "retry"):
            raise RuntimeUnavailable("Invalid native activity")
        return activity["type"] in {"busy", "retry"}

    async def read(
        self, org: str, source_agent: str, agent: str, session_id: str
    ) -> list[dict[str, Any]]:
        self.host.agent(org, source_agent)
        target = self.config.agent(org, agent)
        if target["host_id"] == str(self.host.instance_id):
            return await self.read_local(org, agent, session_id)
        history = await self._request(
            org,
            target["host_id"],
            "/peer/read",
            {"source_agent": source_agent, "agent_id": agent, "session_id": session_id},
        )
        return _validated_history(history, session_id)

    async def _request(self, org: str, host_id: str, path: str, body: dict[str, Any]) -> Any:
        origin, token = self.config.connection(org, host_id)
        try:
            async with httpx.AsyncClient(
                base_url=origin, transport=self.transport, timeout=30, follow_redirects=False
            ) as client:
                response = await client.post(
                    f"/organizations/{org}{path}",
                    headers={"Authorization": f"Bearer {token}"},
                    json=body,
                )
                response.raise_for_status()
                return response.json()
        except (httpx.HTTPError, ValueError):
            raise RuntimeUnavailable("Peer is unavailable") from None


def _chart_distances(roster: list[dict[str, Any]], source: str) -> dict[str, int]:
    edges: dict[str, set[str]] = {agent["agent_id"]: set() for agent in roster}
    for agent in roster:
        manager = agent["reports_to_agent_id"]
        if manager in edges:
            edges[agent["agent_id"]].add(manager)
            edges[manager].add(agent["agent_id"])
    distances = {source: 0}
    queue = deque([source])
    while queue:
        current = queue.popleft()
        for neighbor in edges.get(current, ()):
            if neighbor not in distances:
                distances[neighbor] = distances[current] + 1
                queue.append(neighbor)
    return distances


def _validated_history(history: Any, session_id: str) -> list[dict[str, Any]]:
    if not isinstance(history, list):
        raise RuntimeUnavailable("Invalid native history")
    for message in history:
        if not isinstance(message, dict) or not isinstance(message.get("parts"), list):
            raise RuntimeUnavailable("Invalid native history")
        info = message.get("info")
        if (
            not isinstance(info, dict)
            or not isinstance(info.get("id"), str)
            or info.get("sessionID") != session_id
        ):
            raise RuntimeUnavailable("Native history belongs to another thread")
        for part in message["parts"]:
            if not isinstance(part, dict) or (
                part.get("type") == "text" and not isinstance(part.get("text"), str)
            ):
                raise RuntimeUnavailable("Invalid native history")
    return history


def _codex_peer_history(reply: Any, session_id: str) -> list[dict[str, Any]]:
    """Project textual Codex items into the existing peer-read DTO.

    Peer tools exchange a concise read model; they do not expose a second
    execution API.  The complete native item stream remains available through
    the Codex workspace facade.
    """
    if not isinstance(reply, dict) or not isinstance(reply.get("data"), list):
        raise RuntimeUnavailable("Invalid Codex history")
    projected: list[dict[str, Any]] = []
    for turn in reply["data"]:
        if not isinstance(turn, dict) or not isinstance(turn.get("items"), list):
            raise RuntimeUnavailable("Invalid Codex history")
        for index, item in enumerate(turn["items"]):
            if not isinstance(item, dict):
                raise RuntimeUnavailable("Invalid Codex history")
            kind = item.get("type")
            text = item.get("text")
            if not isinstance(text, str) and isinstance(item.get("content"), list):
                text = "".join(
                    part["text"]
                    for part in item["content"]
                    if isinstance(part, dict)
                    and part.get("type") == "text"
                    and isinstance(part.get("text"), str)
                )
            if kind not in {"userMessage", "agentMessage"} or not isinstance(text, str):
                continue
            projected.append(
                {
                    "info": {
                        "id": str(item.get("id") or f"{turn.get('id', 'turn')}-{index}"),
                        "sessionID": session_id,
                        "role": "user" if kind == "userMessage" else "assistant",
                    },
                    "parts": [{"type": "text", "text": text}],
                }
            )
    return projected
