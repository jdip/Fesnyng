#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/delivery-common.sh"

usage() {
  echo 'Usage: scripts/pr-to-test.sh submit TITLE BODY_FILE [semver:none|semver:patch|semver:minor|semver:major]'
  echo '       scripts/pr-to-test.sh merge PR_NUMBER'
  echo '       scripts/pr-to-test.sh verify PR_NUMBER'
}
[[ $# -gt 0 ]] || { usage; exit 2; }
[[ $1 != --help ]] || { usage; exit 0; }
delivery_init
case $1 in
  submit)
    [[ $# == 3 || $# == 4 ]] || { usage; exit 2; }
    require_clean
    require_feature
    require_identity
    label=${4:-semver:none}
    case $label in semver:none|semver:patch|semver:minor|semver:major) ;; *) fail 'Invalid semver label.' ;; esac
    [[ -f $3 ]] || fail 'Provide a reviewed PR body file.'
    scripts/check.sh
    git fetch origin
    git merge-base --is-ancestor origin/test HEAD || fail 'Merge fresh origin/test into the feature branch, then review and verify the result.'
    git push --set-upstream origin "$branch"
    create_pr test "$branch" "$2" "$3" "$label"
    ;;
  merge)
    [[ $# == 2 && $2 =~ ^[0-9]+$ ]] || { usage; exit 2; }
    require_clean
    require_feature
    require_identity
    scripts/check.sh
    merge_pr "$2" test "$branch" "$(git rev-parse HEAD)"
    verify_merge "$2" test "$branch"
    refresh_primary
    ;;
  verify)
    [[ $# == 2 && $2 =~ ^[0-9]+$ ]] || { usage; exit 2; }
    require_clean
    read_pr "$2"
    [[ $pr_head == codex/* ]] || fail 'Expected a codex/ task PR.'
    verify_merge "$2" test "$pr_head"
    refresh_primary
    ;;
  *) usage; exit 2 ;;
esac
