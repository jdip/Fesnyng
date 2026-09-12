#!/usr/bin/env bash
# Shared native Git/GitHub operations for the two delivery entry points.
set -euo pipefail

delivery_init() {
  cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
  repository=jdip/Fesnyng
  root=$(pwd -P)
  primary=$(cd -- "$(git rev-parse --git-common-dir)/.." && pwd -P)
  approved_name=Sterling-Automation
  approved_email=259149591+Sterling-Automation@users.noreply.github.com
  case $(git remote get-url origin) in
    https://github.com/jdip/Fesnyng|https://github.com/jdip/Fesnyng.git|git@github.com:jdip/Fesnyng.git) ;;
    *) fail 'Origin is not the approved Fesnyng repository.' ;;
  esac
}

fail() { echo "$*" >&2; exit 1; }

require_clean() {
  [[ -z $(git status --porcelain) ]] || fail 'Commit intended changes and preserve unrelated work before delivery.'
}

require_identity() {
  [[ $(git config user.name) == "$approved_name" && $(git config user.email) == "$approved_email" ]] ||
    fail 'Configure the approved repository-local commit identity from SECURITY.md.'
  [[ $(gh api user --jq .login) == "$approved_name" ]] || fail 'The GitHub account differs from the approved delivery identity.'
}

require_feature() {
  branch=$(git branch --show-current)
  [[ $branch == codex/* && $root != "$primary" ]] || fail 'Run from a codex/ feature branch in a separate worktree.'
}

require_primary() {
  [[ $root == "$primary" && $(git branch --show-current) == test ]] || fail 'Run from the primary test checkout.'
  require_clean
}

wait_checks() {
  local pr=$1 count
  count=$(gh pr view "$pr" --repo "$repository" --json statusCheckRollup --jq '.statusCheckRollup | length')
  if [[ $count == 0 ]]; then
    echo 'No hosted checks are configured; repository-local checks are the current gate.'
  else
    gh pr checks "$pr" --repo "$repository" --watch --fail-fast
  fi
}

read_pr() {
  local details
  details=$(gh pr view "$1" --repo "$repository" --json state,baseRefName,headRefName,headRefOid,mergeCommit,isDraft,isCrossRepository \
    --jq '[.state,.baseRefName,.headRefName,.headRefOid,(.mergeCommit.oid // "-"),.isDraft,.isCrossRepository] | @tsv')
  IFS=$'\t' read -r pr_state pr_base pr_head pr_sha pr_merge pr_draft pr_fork <<< "$details"
  [[ $pr_fork == false ]] || fail 'This entry point requires a branch in the target repository.'
}

merge_pr() {
  local pr=$1 base=$2 head=$3 sha=$4
  read_pr "$pr"
  [[ $pr_state == OPEN && $pr_base == "$base" && $pr_head == "$head" && $pr_sha == "$sha" && $pr_draft == false ]] ||
    fail 'PR state, base, head, or reviewed commit differs from the requested merge.'
  wait_checks "$pr"
  gh pr merge "$pr" --repo "$repository" --merge --match-head-commit "$sha" --author-email "$approved_email"
  read_pr "$pr"
  [[ $pr_state == MERGED ]] || fail 'Merge is pending or queued. Preserve the PR and inspect its state before continuing.'
}

verify_revision() {
  local revision=$1 verification_dir
  verification_dir=$(mktemp -d "${TMPDIR:-/tmp}/fesnyng-verify.XXXXXX")
  git worktree add --detach "$verification_dir" "$revision"
  if ! "$verification_dir/scripts/check.sh"; then
    echo "Verification failed; retained detached worktree: $verification_dir" >&2
    return 1
  fi
  git worktree remove -- "$verification_dir"
  echo "Verified committed revision $revision. Confirm runtime acceptance under docs/workflows/pr-to-test.md."
}

verify_merge() {
  local pr=$1 base=$2 expected_head=$3 author_email author_login committer parents
  read_pr "$pr"
  [[ $pr_state == MERGED && $pr_base == "$base" && $pr_head == "$expected_head" && $pr_merge != - ]] ||
    fail 'The PR is not the expected completed merge.'
  git fetch origin
  git merge-base --is-ancestor "$pr_merge" "origin/$base" || fail 'Merged commit is absent from the remote target branch.'
  parents=$(git show -s --format=%P "$pr_merge")
  [[ $parents == *' '* && ${parents#* } != *' '* ]] || fail 'Delivery did not create a two-parent merge commit.'
  author_email=$(git show -s --format=%ae "$pr_merge")
  author_login=$(gh api "repos/$repository/commits/$pr_merge" --jq '.author.login')
  committer=$(git show -s --format=%ce "$pr_merge")
  [[ $author_email == "$approved_email" && $author_login == "$approved_name" ]] || fail 'Published merge author differs from the approved identity; investigate the metadata.'
  [[ $committer == noreply@github.com || $committer == "$approved_email" ]] || fail 'Unexpected published merge committer; investigate the metadata.'
  verify_revision "$pr_merge"
}

refresh_primary() {
  [[ $(git -C "$primary" branch --show-current) == test && -z $(git -C "$primary" status --porcelain) ]] ||
    fail 'Primary checkout has changed or is dirty; preserve it and report incomplete refresh.'
  git -C "$primary" merge-base --is-ancestor HEAD origin/test ||
    fail 'Primary test is ahead of or diverged from origin/test; preserve it and investigate.'
  git -C "$primary" merge --ff-only origin/test
  [[ $(git -C "$primary" rev-parse HEAD) == $(git rev-parse origin/test) ]] ||
    fail 'Primary test does not match the fetched remote revision.'
  "$primary/scripts/check.sh"
  echo "Primary test checkout verified at $(git -C "$primary" rev-parse HEAD)."
}

create_pr() {
  local base=$1 head=$2 title=$3 body=$4 label=$5 existing
  [[ -f $body ]] || fail 'Provide a reviewed PR body file.'
  existing=$(gh pr list --repo "$repository" --state open --base "$base" --head "$head" --json number --jq '.[].number')
  if [[ -n $existing ]]; then
    [[ $existing != *$'\n'* ]] || fail 'Multiple matching open PRs; inspect before continuing.'
    gh pr view "$existing" --repo "$repository" --json url --jq .url
  else
    gh pr create --repo "$repository" --base "$base" --head "$head" --title "$title" --body-file "$body" --label "$label"
  fi
}
