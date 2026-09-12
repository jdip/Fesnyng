"""Installation commands executed with local control-plane state access."""

import argparse
import getpass
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from fesnyng_backend.agent_storage import AgentStore
from fesnyng_backend.control_plane import create_app


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    bootstrap = commands.add_parser("bootstrap", help="Create the first login offline")
    bootstrap.add_argument("--login", required=True)
    bootstrap.add_argument("--name", required=True)
    host = commands.add_parser(
        "register-host", help="Allocate an ops-managed host to an organization"
    )
    host.add_argument("--id", type=UUID, required=True)
    host.add_argument("--name", required=True)
    host.add_argument("--api-url", required=True)
    host.add_argument("--organization", type=UUID, required=True)
    host.add_argument(
        "--token-file", type=Path, help="Private organization/host binding token file"
    )
    args = parser.parse_args(arguments)
    try:
        app = create_app()
        store = app.state.control_store
        if args.command == "bootstrap":
            password = getpass.getpass("New owner password: ")
            if password != getpass.getpass("Confirm password: "):
                raise ValueError("Passwords do not match.")
            store.bootstrap_owner(args.login, args.name, password)
            print("Owner created.")
        else:
            url = urlsplit(args.api_url)
            if (
                url.scheme not in {"http", "https"}
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
                or url.path not in {"", "/"}
            ):
                raise ValueError(
                    "Use an HTTP(S) host origin without credentials, path, query or fragment."
                )
            AgentStore(store).register_host(
                str(args.id),
                args.name,
                args.api_url.rstrip("/"),
                str(args.organization),
            )
            if args.token_file:
                if args.token_file.stat().st_mode & 0o077:
                    raise ValueError("Binding token file must be private")
                AgentStore(store).set_host_credential(
                    str(args.organization), str(args.id), args.token_file.read_text().strip()
                )
            print("Host allocated.")
    except (ValueError, LookupError, RuntimeError, OSError, sqlite3.IntegrityError) as error:
        print(f"Installation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
