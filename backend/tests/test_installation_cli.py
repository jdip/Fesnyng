from uuid import uuid4

from fesnyng_backend.cli import main


def test_cli_bootstraps_once_without_printing_password(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FESNYNG_CONTROL_PLANE_STATE_DIRECTORY", str(tmp_path / "state"))
    monkeypatch.delenv("FESNYNG_CONTROL_PLANE_DATABASE_PATH", raising=False)
    monkeypatch.setattr("fesnyng_backend.cli.getpass.getpass", lambda _: "private example password")
    assert main(["bootstrap", "--login", "owner", "--name", "Owner"]) == 0
    assert main(["bootstrap", "--login", "another", "--name", "Another"]) == 1
    assert "private example password" not in capsys.readouterr().out


def test_installation_cli_allocates_only_an_explicit_host(organization, monkeypatch):
    settings, _, _, org, agents, _ = organization
    monkeypatch.setenv("FESNYNG_CONTROL_PLANE_STATE_DIRECTORY", str(settings.state_directory))
    monkeypatch.setenv("FESNYNG_CONTROL_PLANE_DATABASE_PATH", str(settings.database_path))
    host_id = str(uuid4())
    assert (
        main(
            [
                "register-host",
                "--id",
                host_id,
                "--name",
                "Second host",
                "--api-url",
                "http://127.0.0.1:8002",
                "--organization",
                org.id,
            ]
        )
        == 0
    )
    assert {host["id"] for host in agents.list_hosts(org.id)} >= {host_id}
