import multiprocessing

import pytest

from fesnyng_backend.host_lifecycle import exclusive_host


def try_ownership(path, result):
    try:
        with exclusive_host(path):
            result.send("owned")
    except RuntimeError:
        result.send("rejected")
    finally:
        result.close()


def test_host_database_has_one_live_process_owner(tmp_path):
    database = tmp_path / "host.sqlite3"
    context = multiprocessing.get_context("spawn")
    with exclusive_host(database):
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(target=try_ownership, args=(database, sender))
        process.start()
        sender.close()
        assert receiver.poll(10)
        assert receiver.recv() == "rejected"
        process.join(10)
        assert process.exitcode == 0
        with pytest.raises(RuntimeError, match="already running"), exclusive_host(database):
            pass
    with exclusive_host(database):
        assert database.with_suffix(".sqlite3.host-lock").is_file()


def test_host_startup_recovers_abandoned_operations_only_after_exclusive_ownership(tmp_path):
    from fastapi.testclient import TestClient

    from fesnyng_backend.agent_host import create_app
    from fesnyng_backend.settings import ServiceSettings

    settings = ServiceSettings(
        service="agent-host",
        state_directory=tmp_path / "state",
        database_path=tmp_path / "state/host.sqlite3",
    )
    app = create_app(settings)
    credentials = app.state.credential_store
    credentials.ensure_profile("organization", "profile", "Shared")
    credentials.acquire_operation("organization", "profile", ["login_required"], "login_pending")
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert credentials.profile_status("organization", "profile")["state"] == "refresh_uncertain"
        operation = credentials.acquire_operation(
            "organization", "profile", ["refresh_uncertain"], "login_pending"
        )
        competing = create_app(settings)
        with pytest.raises(RuntimeError, match="already running"), TestClient(competing):
            pass
        with credentials.connect() as connection:
            assert (
                connection.execute(
                    "SELECT operation FROM credential_profiles WHERE profile_id='profile'"
                ).fetchone()[0]
                == operation
            )
    restarted = create_app(settings)
    with TestClient(restarted):
        assert credentials.profile_status("organization", "profile")["state"] == "refresh_uncertain"
