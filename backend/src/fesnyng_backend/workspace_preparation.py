"""Safe names and Git commands for host-owned thread workspaces."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID


@dataclass(frozen=True)
class WorkspacePaths:
    employee_root: Path
    directory: Path
    bare_repository: Path | None


def employee_workspace_root(root: Path, organization_id: str, agent_id: str) -> Path:
    """Build an unambiguous, host-scoped mount location from canonical UUIDs."""
    organization, agent = str(UUID(organization_id)), str(UUID(agent_id))
    resolved = root.resolve()
    if not resolved.is_absolute():
        raise ValueError("Workspace root must be an absolute path")
    return resolved / organization / agent


def workspace_paths(
    root: Path,
    organization_id: str,
    agent_id: str,
    creation_id: str,
    repository_url: str | None,
) -> WorkspacePaths:
    UUID(creation_id)
    employee_root = employee_workspace_root(root, organization_id, agent_id)
    directory = employee_root / "threads" / creation_id
    bare_repository = None
    if repository_url is not None:
        _repository_url(repository_url)
        bare_repository = employee_root / "repositories" / f"{_repository_key(repository_url)}.git"
    return WorkspacePaths(employee_root, directory, bare_repository)


def _repository_key(repository_url: str) -> str:
    return hashlib.sha256(repository_url.encode()).hexdigest()


def _repository_url(repository_url: str) -> None:
    parsed = urlsplit(repository_url)
    if (
        parsed.scheme not in {"https", "http", "ssh"}
        or not parsed.hostname
        or (parsed.scheme in {"https", "http"} and parsed.username is not None)
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Repository URL is not valid for host workspace preparation")


def validate_branch(branch: str) -> None:
    if (
        not branch
        or len(branch) > 500
        or branch.startswith("-")
        or any(character.isspace() or ord(character) < 32 for character in branch)
    ):
        raise ValueError("Checkout branch is not valid")


GIT_PREPARE_SCRIPT = r'''fail() { printf 'error\0%s\0' "$1"; exit 0; }
employee="$1" bare="$2" directory="$3" origin="$4" branch="$5" creation="$6"
export GIT_TERMINAL_PROMPT=0
export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh} -o BatchMode=yes"
test -d "$employee" && test ! -L "$employee" || fail storage
case "$directory" in "$employee"/threads/*) ;; *) fail storage;; esac
test ! -e "$directory" || fail workspace_conflict
mkdir -p "$(dirname "$bare")" "$(dirname "$directory")" || fail storage
if test -e "$bare"; then
    git -C "$bare" rev-parse --is-bare-repository >/dev/null 2>&1 || fail storage
    test "$(git -C "$bare" rev-parse --is-bare-repository 2>/dev/null)" = true || fail storage
    test "$(git -C "$bare" remote get-url origin 2>/dev/null)" = "$origin" || fail storage
else
    git init --bare -q "$bare" >/dev/null 2>&1 || fail storage
    git -C "$bare" remote add origin "$origin" >/dev/null 2>&1 || fail storage
fi
git check-ref-format --branch "$branch" >/dev/null 2>&1 || fail branch
git -C "$bare" ls-remote --exit-code --heads origin "refs/heads/$branch" >/dev/null 2>&1
status=$?
test "$status" = 0 || { test "$status" = 2 && fail branch; fail authentication; }
git -C "$bare" fetch --no-tags origin "+refs/heads/$branch:refs/remotes/fesnyng/$branch" >/dev/null 2>&1 || fail authentication
git -C "$bare" worktree add --detach "$directory" "refs/remotes/fesnyng/$branch" >/dev/null 2>&1 || fail storage
git -C "$directory" switch -c "fesnyng/$creation" >/dev/null 2>&1 || fail storage
revision="$(git -C "$directory" rev-parse --verify HEAD 2>/dev/null)" || fail storage
printf 'prepared\0%s\0%s\0' "$revision" "$directory"'''


GIT_PREPARED_RECONCILE_SCRIPT = r'''fail() { printf 'invalid\0'; exit 0; }
employee="$1" bare="$2" directory="$3" origin="$4" branch="$5" creation="$6" expected_revision="$7"
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null
if [ ! -e "$directory" ]; then printf 'missing\0'; exit 0; fi
test -d "$employee" && test ! -L "$employee" && test -d "$directory" && test ! -L "$directory" || fail
employee="$(realpath -- "$employee")" && bare="$(realpath -- "$bare")" && actual="$(realpath -- "$directory")" || fail
test "$actual" = "$directory" || fail
case "$directory" in "$employee"/threads/*) ;; *) fail;; esac
root="$(git -C "$directory" rev-parse --show-toplevel 2>/dev/null)" || fail
test "$root" = "$directory" || fail
common="$(git -C "$directory" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" || fail
git_dir="$(git -C "$directory" rev-parse --absolute-git-dir 2>/dev/null)" || fail
common="$(realpath -- "$common")" && git_dir="$(realpath -- "$git_dir")" || fail
test "$common" = "$bare" || fail
case "$git_dir" in "$bare"/worktrees/*) ;; *) fail;; esac
test "$(git -C "$directory" config --get remote.origin.url 2>/dev/null)" = "$origin" || fail
revision="$(git -C "$directory" rev-parse --verify HEAD 2>/dev/null)" || fail
if test -n "$expected_revision"; then
    test "$revision" = "$expected_revision" || fail
else
    remote_revision="$(git -C "$bare" rev-parse --verify "refs/remotes/fesnyng/$branch" 2>/dev/null)" || fail
    test "$revision" = "$remote_revision" || fail
fi
if current_branch="$(git -C "$directory" symbolic-ref --quiet --short HEAD 2>/dev/null)"; then
    test "$current_branch" = "fesnyng/$creation" || fail
else
    status=$?
    test "$status" = 1 || fail
    git -C "$directory" switch -c "fesnyng/$creation" >/dev/null 2>&1 || fail
fi
printf 'prepared\0%s\0' "$revision"'''


GIT_FORK_SCRIPT = r"""fail() { exit 1; }
source="$1" destination="$2" employee="$3" branch="$4"
test -d "$source" && test ! -L "$source" && test ! -e "$destination" || fail
root="$(git -C "$source" rev-parse --show-toplevel 2>/dev/null)" || fail
test "$root" = "$source" || fail
common="$(git -C "$source" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" || fail
common="$(realpath -- "$common")" || fail
case "$common" in "$employee"/repositories/*) ;; *) fail;; esac
git -C "$source" worktree add -b "$branch" "$destination" HEAD >/dev/null 2>&1 || fail
tar -C "$source" --exclude=.git -cf - . | tar -C "$destination" -xf - || fail
git -C "$source" diff --name-only --diff-filter=D -z HEAD |
    xargs -0 -r -n1 sh -c 'rm -f -- "$1/$2"' sh "$destination" || fail"""


STANDALONE_GIT_FORK_SCRIPT = r"""fail() { exit 1; }
source="$1" destination="$2" branch="$3"
test -d "$source" && test ! -L "$source" && test ! -e "$destination" || fail
root="$(git -C "$source" rev-parse --show-toplevel 2>/dev/null)" || fail
test "$root" = "$source" || fail
git_dir="$(git -C "$source" rev-parse --absolute-git-dir 2>/dev/null)" || fail
common="$(git -C "$source" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" || fail
git_dir="$(realpath -- "$git_dir")" && common="$(realpath -- "$common")" || fail
case "$git_dir" in "$source"/.git|"$source"/.git/*) ;; *) fail;; esac
case "$common" in "$source"/.git|"$source"/.git/*) ;; *) fail;; esac
git clone --no-local --no-checkout -q "$source" "$destination" >/dev/null 2>&1 || fail
git -C "$destination" checkout -q HEAD >/dev/null 2>&1 || fail
git -C "$destination" switch -c "$branch" >/dev/null 2>&1 || fail
tar -C "$source" --exclude=.git -cf - . | tar -C "$destination" -xf - || fail
git -C "$source" diff --name-only --diff-filter=D -z HEAD |
    xargs -0 -r -n1 sh -c 'rm -f -- "$1/$2"' sh "$destination" || fail"""


FORK_KIND_SCRIPT = r"""base="$(realpath -- "$1")" || exit 1
employee="$(realpath -- "$2")" || exit 1
test "$base" = "$1" && test "$employee" = "$2" && test -d "$base" || exit 1
case "$base" in "$employee"/threads/*|/workspace/*) ;; *) exit 1;; esac
if ! root="$(git -C "$base" rev-parse --show-toplevel 2>/dev/null)"; then printf ordinary; exit 0; fi
test "$root" = "$base" || exit 1
common="$(git -C "$base" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" || exit 1
common="$(realpath -- "$common")" || exit 1
case "$common" in
    "$employee"/repositories/*) printf linked_git;;
    "$base"/.git|"$base"/.git/*) printf standalone_git;;
    *) exit 1;;
esac"""


ORDINARY_FORK_SCRIPT = r'''test -d "$1" && test ! -L "$1" && test ! -e "$2" || exit 1
! find "$1" -name .git -print -quit | grep -q . || exit 1
! find "$1" -type l -print -quit | grep -q . || exit 1
mkdir -p "$2" && cp -a "$1/." "$2/"'''
