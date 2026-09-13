"""Control-plane calls to installation-bound host origins."""

from typing import Any

import httpx

from fesnyng_backend.agent_storage import AgentStore
from fesnyng_backend.host_models import HostAgentConfiguration


class HostUnavailable(RuntimeError):
    pass


class HostRejected(HostUnavailable):
    def __init__(self, status_code: int):
        super().__init__("Agent host rejected the request")
        self.status_code = status_code


class HostClient:
    def __init__(self, agents: AgentStore, transport: httpx.AsyncBaseTransport | None = None):
        self.agents = agents
        self.transport = transport

    async def request(
        self,
        organization_id: str,
        host_id: str,
        path: str,
        *,
        method: str = "GET",
        body: Any = None,
    ) -> Any:
        origin, token = self.agents.host_connection(organization_id, host_id)
        try:
            async with httpx.AsyncClient(
                base_url=origin,
                headers={"Authorization": f"Bearer {token}"},
                timeout=180,
                follow_redirects=False,
                transport=self.transport,
            ) as client:
                response = await client.request(
                    method, f"/organizations/{organization_id}{path}", json=body
                )
        except httpx.HTTPError:
            raise HostUnavailable("Agent host is unreachable") from None
        if not response.is_success:
            if response.status_code in {400, 403, 404, 409, 422}:
                raise HostRejected(response.status_code)
            raise HostUnavailable(f"Agent host operation failed (HTTP {response.status_code})")
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            raise HostUnavailable("Agent host returned invalid JSON") from None

    async def apply(self, organization_id: str, agent_id: str) -> dict[str, Any]:
        agent = self.agents.get_agent(organization_id, agent_id)
        policy = self.agents.get_policy(organization_id)
        profile_id = agent["configuration"]["profile_id"]
        if profile_id:
            profile = next(
                profile
                for profile in self.agents.list_profiles(organization_id)
                if profile["id"] == profile_id
            )
            await self.request(
                organization_id,
                agent["host_id"],
                f"/profiles/{profile_id}",
                method="PUT",
                body={"name": profile["name"]},
            )
        envelope = HostAgentConfiguration(
            host_id=agent["host_id"],
            organization_id=organization_id,
            agent_id=agent_id,
            version=agent["desired_version"],
            name=agent["name"],
            title=agent["title"],
            reports_to_agent_id=agent["reports_to_agent_id"],
            configuration=agent["configuration"],
            policy_version=policy["desired_version"],
            policy=policy["configuration"],
        )
        reply = await self.request(
            organization_id,
            agent["host_id"],
            f"/agents/{agent_id}",
            method="PUT",
            body=envelope.model_dump(mode="json"),
        )
        expected = {
            "host_id": agent["host_id"],
            "organization_id": organization_id,
            "agent_id": agent_id,
            "applied_version": agent["desired_version"],
            "applied_policy_version": policy["desired_version"],
        }
        if not isinstance(reply, dict) or any(
            reply.get(key) != value for key, value in expected.items()
        ):
            raise HostUnavailable("Host acknowledgement does not match configuration")
        self.agents.acknowledge_host(
            organization_id, agent_id, agent["host_id"], agent["desired_version"]
        )
        return self.agents.get_agent(organization_id, agent_id)
