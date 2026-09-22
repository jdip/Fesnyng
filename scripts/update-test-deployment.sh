#!/usr/bin/env bash
# Stable scheduled entry point: select source before running deployment logic.
set -Eeuo pipefail
config_file=${FESNYNG_DEPLOY_CONFIG:-"${XDG_CONFIG_HOME:-$HOME/.config}/fesnyng-test/deployment.env"}
[[ -r $config_file ]] || { echo "Missing private deployment configuration: $config_file" >&2; exit 1; }
export FESNYNG_DEPLOY_CONFIG=$config_file
# shellcheck source=/dev/null
source "$config_file"
log() { TZ=UTC printf '%(%FT%TZ)T updater stage=%s revision=%s %s\n' -1 "$1" "${target_revision:--}" "${2:-}"; }
stage=initialize
trap 'log failed "stage=$stage exit=$?"; exit 1' ERR
exec 9>"$DEPLOY_BASE/update.lock"
flock -n 9 || { log deferred 'reason=another-update-active'; exit 0; }
stage=fetch
git -C "$SOURCE_MIRROR" fetch --quiet origin refs/heads/test:refs/remotes/origin/test
target_revision=$(git -C "$SOURCE_MIRROR" rev-parse --verify 'refs/remotes/origin/test^{commit}')
release=$DEPLOY_BASE/releases/$target_revision
current_revision=
if [[ -L $DEPLOY_BASE/current ]]; then current_revision=$(cat "$DEPLOY_BASE/current/.fesnyng-revision"); fi
stage=source
mkdir -p "$DEPLOY_BASE/releases"
[[ -d $release ]] || git -C "$SOURCE_MIRROR" worktree add --detach "$release" "$target_revision"
[[ $(git -C "$release" rev-parse HEAD) == "$target_revision" ]] || { log failed 'reason=release-revision-mismatch'; exit 1; }
git -C "$release" diff --quiet HEAD -- || { log failed 'reason=modified-release-source'; exit 1; }
git -C "$release" ls-files --error-unmatch scripts/deploy-test-release.sh >/dev/null
stage=deploy
if [[ $target_revision == "$current_revision" ]]; then
  log check 'reason=unchanged-source'
  bash "$release/scripts/deploy-test-release.sh" "$target_revision" --check
else
  log deploy "from=${current_revision:--}"
  bash "$release/scripts/deploy-test-release.sh" "$target_revision" --deploy
fi
