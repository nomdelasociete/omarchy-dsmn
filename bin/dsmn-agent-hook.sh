#!/usr/bin/env bash
# Renew a dsmn lease from an agent hook. Fail open: a hook must never block the agent.
set -u

MINUTES="${DSMN_HOOK_MINUTES:-20}"
MIN_INTERVAL_SECONDS="${DSMN_HOOK_MIN_INTERVAL_SECONDS:-120}"
FALLBACK_SECONDS="${DSMN_HOOK_FALLBACK_SECONDS:-600}"
STATE_DIR="${DSMN_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/dsmn}"
STAMP_PATH="${DSMN_HOOK_STAMP:-$STATE_DIR/last-extend.stamp}"
LOG_PATH="${DSMN_HOOK_LOG:-$STATE_DIR/hook.log}"
LOCK_PATH="$STATE_DIR/extend.lock"
HOOK_AGENT="${DSMN_HOOK_AGENT:-agent}"
HOOK_EVENT="${DSMN_HOOK_EVENT:-}"

mkdir -p "$STATE_DIR" 2>/dev/null || true

log_event() {
  printf '%s %s\n' "$(date -Iseconds)" "$*" >>"$LOG_PATH" 2>/dev/null || true
}

payload="$(timeout 0.4 cat 2>/dev/null || true)"

requester_name() {
  case "$HOOK_AGENT" in
    claude) printf '%s\n' "Claude Code" ;;
    cursor) printf '%s\n' "Cursor" ;;
    codex) printf '%s\n' "Codex" ;;
    grok) printf '%s\n' "Grok Build" ;;
    pi) printf '%s\n' "Pi" ;;
    *) printf '%s\n' "Agent" ;;
  esac
}

hook_context() {
  [[ -n "$payload" ]] || return 0
  DSMN_HOOK_PAYLOAD="$payload" python3 - <<'PY' 2>/dev/null || true
import json, os
raw = os.environ.get("DSMN_HOOK_PAYLOAD") or ""
try:
    root = json.loads(raw)
except Exception:
    raise SystemExit
def walk(value):
    found = [value]
    if isinstance(value, dict):
        for child in value.values():
            found.extend(walk(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(walk(child))
    return found
def first(*keys):
    for item in walk(root):
        if not isinstance(item, dict):
            continue
        for key in keys:
            value = item.get(key)
            if value not in (None, ""):
                return str(value).replace("\n", " ").strip()
    return ""
session = first("session_id", "sessionId", "conversation_id", "conversationId", "transcript_path")
cwd = first("cwd", "project_dir", "project") or os.environ.get("PWD", "")
project = cwd.rstrip("/").split("/")[-1] if cwd else ""
short = ""
if session:
    short = session.rstrip("/").split("/")[-1]
    for suffix in (".jsonl", ".json"):
        if short.endswith(suffix):
            short = short[: -len(suffix)]
    short = short[:8]
if project and project != "." and short:
    print(f"{project} - session {short}")
elif short:
    print(f"session {short}")
elif project and project != ".":
    print(project)
PY
}

should_skip_extend() {
  [[ -f "$STAMP_PATH" ]] || return 1
  local last now
  last="$(tr -dc '0-9' <"$STAMP_PATH" 2>/dev/null || true)"
  [[ -n "$last" ]] || return 1
  now="$(date +%s)"
  (( now - last < MIN_INTERVAL_SECONDS ))
}

fallback_inhibit() {
  date +%s >"$STAMP_PATH" 2>/dev/null || true
  if ! command -v systemd-inhibit >/dev/null 2>&1; then
    log_event "fallback unavailable: systemd-inhibit not found"
    return 0
  fi
  systemd-inhibit --what=sleep:idle:handle-lid-switch --who=dsmn --why="dsmn hook fallback" --mode=block \
    sleep "$FALLBACK_SECONDS" >/dev/null 2>&1 &
  disown "$!" 2>/dev/null || true
  log_event "fallback inhibit started seconds=$FALLBACK_SECONDS"
}

resolve_dsmn() {
  if [[ -n "${DSMN_BIN:-}" && -x "$DSMN_BIN" ]]; then
    printf '%s\n' "$DSMN_BIN"
    return 0
  fi
  local beside="$HOME/.local/bin/dsmn"
  local sibling
  sibling="$(cd "$(dirname "$0")" && pwd)/dsmn"
  if [[ -x "$beside" ]]; then
    printf '%s\n' "$beside"
    return 0
  fi
  if [[ -x "$sibling" ]]; then
    printf '%s\n' "$sibling"
    return 0
  fi
  return 1
}

acquire_extend_lock() {
  local attempts=0
  mkdir -p "$(dirname "$LOCK_PATH")" 2>/dev/null || true
  while ! mkdir "$LOCK_PATH" 2>/dev/null; do
    if [[ -d "$LOCK_PATH" ]] && find "$LOCK_PATH" -prune -mmin +5 2>/dev/null | grep -q .; then
      rm -rf "$LOCK_PATH" 2>/dev/null || true
      continue
    fi
    if (( attempts >= 20 )); then
      log_event "extend lock busy; skipping"
      exit 0
    fi
    attempts=$((attempts + 1))
    sleep 0.05
  done
  trap 'rmdir "$LOCK_PATH" 2>/dev/null || true' EXIT
}

REQUESTER_NAME="$(requester_name)"
REQUESTER_CONTEXT="$(hook_context)"
acquire_extend_lock

if should_skip_extend; then
  log_event "dsmn extend throttled seconds=$MIN_INTERVAL_SECONDS requester=$REQUESTER_NAME event=$HOOK_EVENT"
  exit 0
fi

dsmn="$(resolve_dsmn || true)"
if [[ -z "$dsmn" ]]; then
  log_event "dsmn not found"
  fallback_inhibit
  exit 0
fi

args=(manual --minutes "$MINUTES" --extend-only --requester "$REQUESTER_NAME")
[[ -n "$REQUESTER_CONTEXT" ]] && args+=(--context "$REQUESTER_CONTEXT")
[[ -n "$HOOK_EVENT" ]] && args+=(--event "$HOOK_EVENT")

if ! "$dsmn" "${args[@]}" >/dev/null 2>&1; then
  log_event "dsmn manual failed requester=$REQUESTER_NAME"
  fallback_inhibit
  exit 0
fi

date +%s >"$STAMP_PATH" 2>/dev/null || true
log_event "dsmn active minutes=$MINUTES requester=$REQUESTER_NAME context=$REQUESTER_CONTEXT event=$HOOK_EVENT"
exit 0
