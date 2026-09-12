from pathlib import Path

import pytest

from fesnyng_backend.control_store import BootstrapAlreadyComplete, ControlPlaneStore


def test_offline_owner_bootstrap_creates_exactly_one_initial_user(tmp_path: Path):
    store = ControlPlaneStore(tmp_path / "control-plane.sqlite3")
    store.initialize()

    owner = store.bootstrap_owner(
        login="owner",
        display_name="Initial Owner",
        password="a-long-test-password",
    )

    assert owner.login == "owner"
    assert owner.display_name == "Initial Owner"
    with pytest.raises(BootstrapAlreadyComplete):
        store.bootstrap_owner(
            login="second-owner",
            display_name="Second Owner",
            password="another-long-test-password",
        )


def test_concurrent_bootstrap_allows_only_one_owner(tmp_path: Path):
    import threading

    database_path = tmp_path / "control-plane.sqlite3"
    first = ControlPlaneStore(database_path)
    second = ControlPlaneStore(database_path)
    first.initialize()
    outcomes: list[object] = []
    barrier = threading.Barrier(2)

    def bootstrap(store: ControlPlaneStore, login: str) -> None:
        barrier.wait()
        try:
            outcomes.append(store.bootstrap_owner(login, login, "a-long-test-password"))
        except BootstrapAlreadyComplete as error:
            outcomes.append(error)

    threads = [
        threading.Thread(target=bootstrap, args=(first, "ownerone")),
        threading.Thread(target=bootstrap, args=(second, "ownertwo")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(not isinstance(outcome, BootstrapAlreadyComplete) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, BootstrapAlreadyComplete) for outcome in outcomes) == 1
