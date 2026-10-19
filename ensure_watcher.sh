#!/usr/bin/env bash
# Start inbox_watcher.py in the background if it is not already running, then
# print its status. Safe to call often (from cron, a heartbeat job or a login
# script): the watcher's own pid lock keeps a single instance.
# Extra arguments (e.g. --allow-insecure-http) are passed to the watcher.
# Uses the current environment (WAKE_URL, WAKE_AUTH, AIW_*). Never echoes them.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$HERE/inbox_watcher.py"
STATE="${AIW_STATE_DIR:-$PWD/state}"
mkdir -p "$STATE"
if python3 "$PY" --status >/dev/null 2>&1; then
  echo "inbox_watcher already running."
else
  if [ -z "${WAKE_URL:-}" ] || [ -z "${WAKE_AUTH:-}" ]; then
    echo "WARNING: WAKE_URL and/or WAKE_AUTH not set; the watcher will run in DRY-RUN mode."
  fi
  AIW_STATE_DIR="$STATE" nohup python3 "$PY" "$@" >>"$STATE/watcher.nohup.out" 2>&1 </dev/null &
  sleep 2
  echo "inbox_watcher started."
fi
AIW_STATE_DIR="$STATE" python3 "$PY" --status
