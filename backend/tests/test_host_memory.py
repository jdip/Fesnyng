import secrets
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest

from fesnyng_backend.host_memory import MemoryStore
from fesnyng_backend.host_models import Actor, HostAgentConfiguration
from fesnyng_backend.host_store import HostStore
from fesnyng_backend.settings import ServiceSettings


def test_memory_persists_content_and_actor_provenance_after_restart(tmp_path):
    settings = ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "host.sqlite3",
        state_directory=tmp_path / "state",
    )
    host = HostStore(settings)
    host.initialize()
    organization_id, agent_id = str(uuid4()), str(uuid4())
    host.bind_organization(organization_id, secrets.token_urlsafe(32))
    host.stage_agent(
        HostAgentConfiguration(
            host_id=host.instance_id,
            organization_id=organization_id,
            agent_id=agent_id,
            version=1,
            name="Memory agent",
        )
    )
    author = Actor(kind="human", id=uuid4(), name="Owner", session_id="native-session")

    memory = MemoryStore(host)
    memory.initialize()
    saved = memory.put(
        organization_id,
        agent_id,
        "preferences",
        "Use concise review summaries.",
        0,
        author,
    )

    assert saved["key"] == "preferences"
    assert saved["content"] == "Use concise review summaries."
    assert saved["revision"] == 1
    assert saved["author"] == {
        "kind": "human",
        "id": str(author.id),
        "name": "Owner",
        "session_id": "native-session",
    }
    assert isinstance(saved["updated_at"], int)

    restarted = MemoryStore(HostStore(settings))
    restarted.initialize()
    assert restarted.get(organization_id, agent_id, "preferences") == saved


def test_memory_isolated_by_organization_and_agent(tmp_path):
    settings = ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "host.sqlite3",
        state_directory=tmp_path / "state",
    )
    host = HostStore(settings)
    host.initialize()
    first_org, first_agent, second_org, second_agent = (str(uuid4()) for _ in range(4))
    for organization_id, agent_id in ((first_org, first_agent), (second_org, second_agent)):
        host.bind_organization(organization_id, secrets.token_urlsafe(32))
        host.stage_agent(
            HostAgentConfiguration(
                host_id=host.instance_id,
                organization_id=organization_id,
                agent_id=agent_id,
                version=1,
                name="Memory agent",
            )
        )
    memory = MemoryStore(host)
    memory.initialize()
    author = Actor(kind="agent", id=uuid4(), name="Writer")

    memory.put(first_org, first_agent, "context", "First organization", 0, author)
    memory.put(second_org, second_agent, "context", "Second organization", 0, author)

    assert [record["content"] for record in memory.list(first_org, first_agent)] == [
        "First organization"
    ]
    assert [record["content"] for record in memory.list(second_org, second_agent)] == [
        "Second organization"
    ]
    with pytest.raises(LookupError):
        memory.get(first_org, second_agent, "context")


def test_concurrent_stale_memory_updates_allow_one_revision_increment(tmp_path):
    settings = ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "host.sqlite3",
        state_directory=tmp_path / "state",
    )
    host = HostStore(settings)
    host.initialize()
    organization_id, agent_id = str(uuid4()), str(uuid4())
    host.bind_organization(organization_id, secrets.token_urlsafe(32))
    host.stage_agent(
        HostAgentConfiguration(
            host_id=host.instance_id,
            organization_id=organization_id,
            agent_id=agent_id,
            version=1,
            name="Memory agent",
        )
    )
    memory = MemoryStore(host)
    memory.initialize()
    author = Actor(kind="human", id=uuid4(), name="Owner")
    memory.put(organization_id, agent_id, "context", "Original", 0, author)
    start = Barrier(2)

    def update(content: str):
        start.wait()
        try:
            return memory.put(organization_id, agent_id, "context", content, 1, author)
        except ValueError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(update, ["First update", "Second update"]))

    saved = [outcome for outcome in outcomes if isinstance(outcome, dict)]
    conflicts = [outcome for outcome in outcomes if isinstance(outcome, ValueError)]
    assert len(saved) == 1 and saved[0]["revision"] == 2
    assert [str(error) for error in conflicts] == ["Memory revision conflict"]
    current = memory.get(organization_id, agent_id, "context")
    assert current is not None and current["revision"] == 2
    assert current["content"] in {"First update", "Second update"}
