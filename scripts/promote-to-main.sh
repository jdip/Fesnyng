#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/delivery-common.sh"

usage() {
  echo 'Requires a separate explicit operator promotion request.'
  echo 'Usage: scripts/promote-to-main.sh submit TITLE BODY_FILE'
  echo '       scripts/promote-to-main.sh merge PR_NUMBER'
  echo '       scripts/promote-to-main.sh sync SYNC_PR_NUMBER REVIEWED_MAIN_SHA'
  echo '       scripts/promote-to-main.sh verify PROMOTION_PR_NUMBER SYNC_PR_NUMBER'
}
[[ $# -gt 0 ]] || { usage; exit 2; }
[[ $1 != --help ]] || { usage; exit 0; }
delivery_init
require_primary
require_identity
case $1 in
  submit)
    [[ $# == 3 ]] || { usage; exit 2; }
    git fetch origin
    git merge --ff-only origin/test
    scripts/check.sh
    git merge-base --is-ancestor origin/main origin/test || fail 'Main has unsynchronized changes; investigate and use a reviewed main-to-test synchronization PR first.'
    [[ $(git rev-parse origin/main) != $(git rev-parse origin/test) ]] || fail 'No test changes await promotion.'
    create_pr main test "$2" "$3" semver:none
    ;;
  merge)
    [[ $# == 2 && $2 =~ ^[0-9]+$ ]] || { usage; exit 2; }
    git fetch origin
    [[ $(git rev-parse HEAD) == $(git rev-parse origin/test) ]] || fail 'Refresh and review the current test revision before promotion.'
    scripts/check.sh
    merge_pr "$2" main test "$(git rev-parse HEAD)"
    verify_merge "$2" main test
    synchronization_body=$(mktemp "${TMPDIR:-/tmp}/fesnyng-sync.XXXXXX")
    cat > "$synchronization_body" <<EOF
Synchronize the verified main promotion from pull request #$2 back to test using a merge commit. This preserves ancestry for subsequent task delivery. Synchronization contributes no semantic version increment.
EOF
    create_pr test main 'Synchronize main into test after promotion' "$synchronization_body" semver:none
    rm -- "$synchronization_body"
    echo 'Review the synchronization diff, then run sync with its PR number and reviewed main SHA.'
    ;;
  sync)
    [[ $# == 3 && $2 =~ ^[0-9]+$ && $3 =~ ^[0-9a-f]{40}$ ]] || { usage; exit 2; }
    git fetch origin
    [[ $(git rev-parse origin/main) == "$3" ]] || fail 'Main has changed since synchronization review; review the new revision first.'
    verify_revision "$3"
    merge_pr "$2" test main "$3"
    verify_merge "$2" test main
    refresh_primary
    ;;
  verify)
    [[ $# == 3 && $2 =~ ^[0-9]+$ && $3 =~ ^[0-9]+$ ]] || { usage; exit 2; }
    verify_merge "$2" main test
    promoted_sha=$pr_merge
    verify_merge "$3" test main
    git merge-base --is-ancestor "$promoted_sha" "$pr_merge" || fail 'Synchronization does not include the selected promotion.'
    refresh_primary
    ;;
  *) usage; exit 2 ;;
esac
