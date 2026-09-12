from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, current_thread

from fesnyng_backend import control_store


def test_old_password_login_cannot_issue_a_session_after_password_change(organization, monkeypatch):
    _, store, owner, _, _, _ = organization
    validated = Event()
    resume = Event()
    original = control_store.verify_password

    def delayed_verify(password, encoded):
        result = original(password, encoded)
        if current_thread().name.startswith("old-login"):
            validated.set()
            assert resume.wait(10)
        return result

    monkeypatch.setattr(control_store, "verify_password", delayed_verify)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="old-login") as pool:
        pending = pool.submit(store.login, "owner", "correct horse battery staple", 3600)
        try:
            assert validated.wait(10)
            replacement = store.change_password(
                owner.id, "correct horse battery staple", "a new owner password", 3600
            )
            assert replacement is not None
        finally:
            resume.set()
        assert pending.result() is None
    assert store.user_for_session(replacement.token) == owner


def test_concurrent_password_changes_cannot_overwrite_a_changed_password(organization, monkeypatch):
    _, store, owner, _, _, _ = organization
    barrier = Barrier(2)
    original = control_store.verify_password

    def simultaneous_verify(password, encoded):
        result = original(password, encoded)
        barrier.wait(timeout=10)
        return result

    monkeypatch.setattr(control_store, "verify_password", simultaneous_verify)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [
            pool.submit(
                store.change_password, owner.id, "correct horse battery staple", password, 3600
            )
            for password in ("first replacement password", "second replacement password")
        ]
        results = [future.result() for future in pending]
    assert sum(result is not None for result in results) == 1
    successful = next(result for result in results if result is not None)
    assert store.user_for_session(successful.token) == owner
