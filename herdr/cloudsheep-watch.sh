#!/usr/bin/env bash
# Keep one background `cloudsheep watch` running (herdr notifications for job
# completion and lease expiry). Usage: cloudsheep-watch.sh [start|stop|status|toggle]
set -uo pipefail

here="$(cd "$(dirname "$(realpath "$0")")" && pwd)"
cs="${CLOUDSHEEP_BIN:-$here/../bin/cloudsheep}"
state="${CLOUDSHEEP_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/cloudsheep}"
pidfile="$state/watch.pid"
log="$state/watch.log"
mkdir -p "$state"

running() { [[ -f $pidfile ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; }
notify() { "${HERDR_BIN_PATH:-herdr}" notification show "🐑 cloudsheep" --body "$1" >/dev/null 2>&1 || echo "$1"; }

start() {
  if running; then notify "watch already running"; return; fi
  nohup "$cs" watch --notify auto "$@" >>"$log" 2>&1 </dev/null &
  echo $! >"$pidfile"
  notify "watching machines (log: $log)"
}

stop() {
  if running; then kill "$(cat "$pidfile")"; rm -f "$pidfile"; notify "watch stopped"; else notify "watch not running"; fi
}

case "${1:-toggle}" in
  start) shift; start "$@" ;;
  stop) stop ;;
  status) if running; then echo "running (pid $(cat "$pidfile"))"; else echo "stopped"; fi ;;
  toggle) if running; then stop; else start; fi ;;
  *) echo "usage: $0 [start|stop|status|toggle]" >&2; exit 2 ;;
esac
