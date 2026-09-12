import asyncio
import base64
import json
import stat
from pathlib import Path

import httpx
import pytest

from fesnyng_backend.host_credentials import CredentialService, CredentialStore


def _jwt(**claims: object) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


def _tokens(
    account_id: str, *, access: str = "access", expires_in: int = 3600
) -> dict[str, object]:
    return {
        "access_token": _jwt(chatgpt_account_id=account_id),
        "refresh_token": "refresh-value",
        "expires_in": expires_in,
    }


def _ready_store(tmp_path: Path) -> tuple[CredentialStore, str]:
    store = CredentialStore(tmp_path / "host.sqlite3")
    store.initialize()
    store.ensure_profile("organization-a", "profile-a", "Primary profile")
    store.assign_agent("organization-a", "agent-a", "profile-a", "agent-key")
    operation = store.acquire_operation("organization-a", "profile-a", ["login_required"], "login")
    assert operation is not None
    store.save_tokens("organization-a", "profile-a", _tokens("account-a"), operation)
    return store, "agent-key"


def test_new_credential_database_and_parent_are_private(tmp_path: Path) -> None:
    database = tmp_path / "host-state" / "credentials.sqlite3"
    CredentialStore(database).initialize()

    assert stat.S_IMODE(database.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(database.stat().st_mode) == 0o600


def test_credential_store_rejects_public_existing_parent_without_changing_it(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "public-state"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    database = parent / "credentials.sqlite3"

    with pytest.raises(ValueError, match="directory must be private"):
        CredentialStore(database).initialize()

    assert stat.S_IMODE(parent.stat().st_mode) == 0o755
    assert not database.exists()


def test_credential_store_rejects_public_existing_database_without_changing_it(
    tmp_path: Path,
) -> None:
    database = tmp_path / "credentials.sqlite3"
    database.touch(mode=0o644)
    database.chmod(0o644)

    with pytest.raises(ValueError, match="database must be private"):
        CredentialStore(database).initialize()

    assert stat.S_IMODE(database.stat().st_mode) == 0o644


def test_credential_access_is_persistent_assigned_and_redacted(tmp_path: Path) -> None:
    async def exercise() -> None:
        store, agent_key = _ready_store(tmp_path)

        credential = await CredentialService(store, httpx.AsyncClient()).access_for_agent(agent_key)
        persisted = CredentialStore(tmp_path / "host.sqlite3")
        persisted.initialize()
        restored = await CredentialService(persisted, httpx.AsyncClient()).access_for_agent(
            agent_key
        )

        assert credential == restored
        assert credential["profile_id"] == "profile-a"
        assert credential["account_id"] == "account-a"
        assert "refresh" not in credential
        assert "agent-key" not in (tmp_path / "host.sqlite3").read_text(errors="ignore")
        persisted.unassign_agent("organization-a", "agent-a")
        with pytest.raises(PermissionError, match="unavailable"):
            await CredentialService(persisted, httpx.AsyncClient()).access_for_agent(agent_key)

    asyncio.run(exercise())


def test_profile_scope_is_immutable_and_account_changes_fail_closed(tmp_path: Path) -> None:
    store, _ = _ready_store(tmp_path)

    with pytest.raises(ValueError, match="organization"):
        store.ensure_profile("organization-b", "profile-a", "Other organization")
    with pytest.raises(ValueError, match="profile"):
        store.assign_agent("organization-b", "agent-b", "profile-a", "other-key")

    operation = store.acquire_operation("organization-a", "profile-a", ["ready"], "refreshing")
    assert operation is not None
    with pytest.raises(PermissionError, match="account"):
        store.save_tokens("organization-a", "profile-a", _tokens("account-b"), operation)

    status = store.profile_status("organization-a", "profile-a")
    assert status is not None
    assert status["state"] == "account_mismatch"
    assert set(status) == {
        "organization_id",
        "profile_id",
        "name",
        "account_id",
        "expires",
        "residency",
        "generation",
        "state",
    }


def test_missing_account_claims_fail_closed(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / "host.sqlite3")
    store.initialize()
    store.ensure_profile("organization-a", "profile-a", "Primary profile")
    operation = store.acquire_operation("organization-a", "profile-a", ["login_required"], "login")
    assert operation is not None

    with pytest.raises(PermissionError, match="claims"):
        store.save_tokens(
            "organization-a",
            "profile-a",
            {"access_token": _jwt(), "refresh_token": "refresh-value", "expires_in": 3600},
            operation,
        )
    status = store.profile_status("organization-a", "profile-a")
    assert status is not None
    assert status["state"] == "account_mismatch"


def test_twenty_concurrent_agent_accesses_refresh_once(tmp_path: Path) -> None:
    async def exercise() -> None:
        store, agent_key = _ready_store(tmp_path)
        with store.connect() as connection:
            connection.execute(
                "UPDATE credential_profiles SET expires=0 WHERE organization_id=? AND profile_id=?",
                ("organization-a", "profile-a"),
            )
        calls = 0

        async def refresh(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            assert request.url == "https://auth.openai.com/oauth/token"
            await asyncio.sleep(0)
            return httpx.Response(200, json=_tokens("account-a", access="refreshed"))

        async with httpx.AsyncClient(transport=httpx.MockTransport(refresh)) as client:
            service = CredentialService(store, client)
            credentials = await asyncio.gather(
                *(service.access_for_agent(agent_key) for _ in range(20))
            )

        assert calls == 1
        status = store.profile_status("organization-a", "profile-a")
        assert status is not None
        assert status["generation"] == 2
        assert {credential["generation"] for credential in credentials} == {2}

    asyncio.run(exercise())


def test_uncertain_refresh_blocks_agent_access(tmp_path: Path) -> None:
    async def exercise() -> None:
        store, agent_key = _ready_store(tmp_path)
        with store.connect() as connection:
            connection.execute(
                "UPDATE credential_profiles SET expires=0 WHERE organization_id=? AND profile_id=?",
                ("organization-a", "profile-a"),
            )

        def unavailable(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("offline", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
            service = CredentialService(store, client)
            with pytest.raises(PermissionError, match="reconciliation"):
                await service.refresh_profile("organization-a", "profile-a")
            with pytest.raises(PermissionError, match="unavailable"):
                await service.access_for_agent(agent_key)
        status = store.profile_status("organization-a", "profile-a")
        assert status is not None
        assert status["state"] == "refresh_uncertain"

    asyncio.run(exercise())
