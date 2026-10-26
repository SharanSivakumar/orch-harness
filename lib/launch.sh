#!/usr/bin/env bash
# launch.sh — runs INSIDE a session's tmux window: <sid> <executor> <work-order>.
#
# The engine passes the work order's path explicitly (no flavor lookup here) and
# ORCH_INSTANCE / ORCH_ROW / ORCH_KIND / ORCH_PLAN_DIR in the environment. This
# script exports the rig's configuration, the executor's own args and env
# (orch.yaml executors.args / executors.env), then execs the adapter. An executor VARIANT
# (executors.variants) runs through its base's adapter, which is handed the base's name.
#
# v1 fell back to `exec bash` when the work order or adapter was missing, which
# read as RUNNING forever. A launch failure now writes a BLOCKED claim and exits,
# so the engine sees it on the next tick.
set -u
sid=$1 x=$2 order=$3
LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fail(){
  echo "[orch] LAUNCH FAILED for $sid: $*" >&2
  if [[ -n ${ORCH_INSTANCE:-} && -d $ORCH_INSTANCE/state ]]; then
    case ${ORCH_KIND:-row} in
      unblock)   v=ESCALATE ;;
      review)    v=FAILED ;;
      preflight) v=FIX ;;
      roundreview) v=FAILED ;;
      steward)   v=ESCALATE ;;
      planner)   v=FAIL ;;
      *)         v=BLOCKED ;;
    esac
    printf '%s\nlaunch failed: %s\n' "$v" "$*" > "$ORCH_INSTANCE/state/$sid.status"
  fi
  sleep 5; exit 1
}
_cfg="$(python3 "$LIB/config_parser.py" --sh)" || fail "orch.yaml could not be loaded"
eval "$_cfg"
export $(compgen -v ORCH_)
_l="$(python3 "$LIB/config_parser.py" --launch "$x")" || fail "no launch settings for executor '$x'"
eval "$_l"
[[ -s $order ]] || fail "no work order at $order"
[[ -x $ORCH_ADAPTER ]] || fail "no adapter $ORCH_ADAPTER for executor '$x'"
cd "$ORCH_ROOT" || fail "cannot cd to $ORCH_ROOT"
# v3: the CLI runs as a child so its own exit is recorded (state/<sid>.exit "<rc> <epoch>"). A window
# killed from outside (tmux kill-window, a lost tmux server, a reboot) HUPs this shell first and leaves
# no record — the engine tells "died by itself" (an infra suspect when fast) from "was killed".
# The adapter runs in the background and this shell `wait`s for it, so a HUP or TERM is handled at once
# (a foreground child would defer the trap until it exits, and a TUI such as OpenCode ignores SIGHUP):
# the adapter's whole process tree (descendants that lead their own session too; `orch longrun` jobs
# stay, as in tmuxio.kill) gets SIGTERM, SIGKILL after a short grace, then exit 129 with no record.
adapter_pid=
export ORCH_LAUNCH_TAG="$sid.$$.$(date +%s%N)"   # finds the adapter's orphaned helpers by environment
stop_tree(){
  trap '' HUP TERM
  if [[ -n $adapter_pid ]]; then
    ( trap - TERM; trap '' HUP; exec python3 "$LIB/tmuxio.py" killtree "$adapter_pid" "${ORCH_KILL_GRACE:-5}" \
        "$ORCH_LAUNCH_TAG" ) >/dev/null 2>&1
  fi
  exit 129
}
trap stop_tree HUP TERM
exec 3<&0
( trap - INT QUIT; exec "$ORCH_ADAPTER" "$sid" "${ORCH_ADAPTER_X:-$x}" "$order" ) <&3 &
adapter_pid=$!
exec 3<&-
wait "$adapter_pid"
rc=$?
adapter_pid=
printf '%s %s\n' "$rc" "$(date +%s)" > "$ORCH_INSTANCE/state/$sid.exit" 2>/dev/null
exit $rc
