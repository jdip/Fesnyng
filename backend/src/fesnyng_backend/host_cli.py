"""Local installation authority for one autonomous agent host."""

import argparse
import sys
from pathlib import Path
from uuid import UUID

from fesnyng_backend.host_store import HostStore
from fesnyng_backend.settings import settings_from_environment


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    bind = commands.add_parser("bind-organization")
    bind.add_argument("--organization", type=UUID, required=True)
    bind.add_argument("--token-file", type=Path, required=True)
    args = parser.parse_args(arguments)
    try:
        if args.token_file.stat().st_mode & 0o077:
            raise ValueError("Binding token file must be private")
        store = HostStore(settings_from_environment("agent-host"))
        store.initialize()
        store.bind_organization(str(args.organization), args.token_file.read_text().strip())
        print("Organization bound to host.")
    except (ValueError, OSError, RuntimeError) as error:
        print(f"Host installation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
