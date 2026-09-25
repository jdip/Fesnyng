#!/usr/bin/env bash
set -Eeuo pipefail
config_file=${FESNYNG_DEPLOY_CONFIG:-"${XDG_CONFIG_HOME:-$HOME/.config}/fesnyng-test/deployment.env"}
[[ -r $config_file ]] || { echo "Missing private deployment configuration: $config_file" >&2; exit 1; }
# shellcheck source=/dev/null
source "$config_file"
started=$SECONDS
log() { TZ=UTC printf '%(%FT%TZ)T deployment stage=%s revision=%s duration=%ss %s\n' -1 "$1" "${2:--}" "$((SECONDS-started))" "${3:-}"; }
stage=initialize
trap 'log failed "${target_revision:--}" "stage=$stage exit=$?"; exit 1' ERR
[[ $# == 2 && $1 =~ ^[0-9a-f]{40}$ && ( $2 == --deploy || $2 == --check ) ]] || { echo 'Run the installed update wrapper to select and lock a test revision.' >&2; exit 2; }
target_revision=$1
# The wrapper holds this descriptor throughout selection and deployment.
[[ /proc/$$/fd/9 -ef $DEPLOY_BASE/update.lock ]] && flock -n 9 || { log failed "$target_revision" 'reason=missing-update-lock'; exit 1; }
release=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
[[ $release -ef "$DEPLOY_BASE/releases/$target_revision" && $(git -C "$release" rev-parse HEAD) == "$target_revision" ]] || { log failed "$target_revision" 'reason=release-revision-mismatch'; exit 1; }
read_revision() { [[ -r $1/.fesnyng-revision ]] && cat "$1/.fesnyng-revision"; }
write_curl_config() { umask 077; printf 'header = "Authorization: Bearer %s"\n' "$(<"$MAINTENANCE_TOKEN_FILE")" > "$MAINTENANCE_CURL_CONFIG"; }
maintenance() {
  local action=$1 body status payload state reason timeout=20
  [[ $action != rollout ]] || timeout=1800
  body=$(mktemp "${XDG_RUNTIME_DIR:-/tmp}/fesnyng-maintenance.XXXXXX")
  status=$(curl --connect-timeout 5 --max-time "$timeout" --silent --show-error --output "$body" --write-out '%{http_code}' --config "$MAINTENANCE_CURL_CONFIG" -X POST "http://127.0.0.1:$HOST_PORT/maintenance/$action" || true)
  payload=$(<"$body"); rm -f -- "$body"
  state=$(python3 -c 'import json,sys; value=json.load(sys.stdin); print(value.get("state", ""))' <<<"$payload" 2>/dev/null || true)
  reason=$(python3 -c 'import json,sys; value=json.load(sys.stdin); print(value.get("reason", ""))' <<<"$payload" 2>/dev/null || true)
  [[ $action == acquire && $status == 200 && $state == closed ]] && return 0
  [[ $action == rollout && $status == 200 && $state == closed ]] && return 0
  [[ $action == release && $status == 200 && $state == open ]] && return 0
  if [[ $action == acquire && $status == 409 && $state == open ]]; then
    case $reason in
      'credential operation'|'pending delivery'|'pending peer work'|'pending interaction'|'native activity is busy or unavailable') log deferred "$target_revision" "reason=$reason";;
      *) log deferred "$target_revision" 'reason=host-busy';;
    esac
    return 2
  fi
  log maintenance "$target_revision" "result=http-$status"
  return 1
}
maintenance_is_open() {
  local payload state
  write_curl_config
  payload=$(curl --connect-timeout 5 --max-time 20 --fail --silent --show-error --config "$MAINTENANCE_CURL_CONFIG" "http://127.0.0.1:$HOST_PORT/maintenance") || return 1
  state=$(python3 -c 'import json,sys; value=json.load(sys.stdin); print(value.get("state", ""))' <<<"$payload" 2>/dev/null || true)
  [[ $state == open ]]
}
health_has_revision() {
  local health port=$1
  for _ in {1..30}; do
    health=$(curl --connect-timeout 5 --max-time 10 --fail --silent --show-error "http://127.0.0.1:$port/health" 2>/dev/null || true)
    if [[ $health == *"\"revision\":\"$target_revision\""* ]]; then log health "$target_revision" "port=$port result=ok"; return 0; fi
    sleep 1
  done
  log health "$target_revision" "port=$port result=failed"
  return 1
}
configure_shutdown() {
  # Existing installed launchers source this private file on every service start.
  # Preserve all operator configuration and avoid silently overriding a custom bound.
  local setting='export UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN=10' updated
  grep -qxF "$setting" "$config_file" && return 0
  if grep -q 'UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN' "$config_file"; then
    log failed "$target_revision" 'reason=conflicting-graceful-shutdown-setting'
    return 1
  fi
  updated=$(mktemp "$config_file.XXXXXX")
  if ! cp -p -- "$config_file" "$updated" ||
     ! printf '\n%s\n' "$setting" >> "$updated" ||
     ! mv -f -- "$updated" "$config_file"; then
    rm -f -- "$updated"
    return 1
  fi
}
prepare() {
  stage=shutdown-configuration
  configure_shutdown
  stage=dependencies
  log start "$target_revision" 'stage=dependencies'
  (cd "$release"; "$UV_BIN" sync --locked --project backend; npm ci --prefix frontend; npm ci --prefix agent-runtime)
  log complete "$target_revision" 'stage=dependencies'
  stage=frontend-build
  log start "$target_revision" 'stage=frontend-build'
  (cd "$release"; npm run build --prefix frontend)
  log complete "$target_revision" 'stage=frontend-build'
  stage=runtime-build
  log start "$target_revision" 'stage=runtime-build'
  (cd "$release"; docker build -t "fesnyng-agent:prepared-$target_revision" agent-runtime)
  log complete "$target_revision" 'stage=runtime-build'
  printf '%s\n' "$target_revision" > "$release/.fesnyng-revision"
}
activate() {
  local initial=$1
  if [[ $initial == false ]]; then
    stage=acquire; write_curl_config
    if maintenance acquire; then :; else outcome=$?; [[ $outcome == 2 ]] && { log deferred "$target_revision" 'reason=host-busy'; return; }; return "$outcome"; fi
  fi
  stage=activate
  if [[ -e $DEPLOY_BASE/current.next && ! -L $DEPLOY_BASE/current.next ]]; then
    log failed "$target_revision" 'reason=unexpected-activation-path'
    return 1
  fi
  ln -sfnT "$release" "$DEPLOY_BASE/current.next"
  mv -Tf "$DEPLOY_BASE/current.next" "$DEPLOY_BASE/current"
  stage=restart; log start "$target_revision" 'stage=restart'; systemctl --user restart fesnyng-test-control.service fesnyng-test-host.service; log complete "$target_revision" 'stage=restart'
  stage=health; if ! health_has_revision "$CONTROL_PORT" || ! health_has_revision "$HOST_PORT"; then return 1; fi
  if [[ $initial == false ]]; then
    stage=runtime-rollout; log start "$target_revision" 'stage=runtime-rollout'
    maintenance rollout
    log complete "$target_revision" 'stage=runtime-rollout'
    stage=health; if ! health_has_revision "$CONTROL_PORT" || ! health_has_revision "$HOST_PORT"; then return 1; fi
    stage=release; maintenance release
  fi
  log complete "$target_revision" 'result=healthy'
}
current_revision=
if [[ -L $DEPLOY_BASE/current ]]; then current_revision=$(read_revision "$(readlink -f "$DEPLOY_BASE/current")"); fi
if [[ $target_revision == "$current_revision" ]]; then
  if maintenance_is_open && health_has_revision "$CONTROL_PORT" && health_has_revision "$HOST_PORT"; then log unchanged "$target_revision"; exit 0; fi
  log failed "$target_revision" 'reason=current-release-unhealthy-or-recovery-required'; exit 1
fi
[[ $2 == --deploy ]] || { log failed "$target_revision" 'reason=current-release-changed'; exit 1; }
log prepare "$target_revision" "from=${current_revision:--}"; prepare
activate $([[ -z $current_revision ]] && echo true || echo false)
