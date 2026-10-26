#!/usr/bin/env bash
# run_all.sh — every framework test. On a clean tree that passes, stamps
# .tested/<commit> so `orch start` can tell a tested framework from an untested one.
set -u
HOME_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HOME_DIR" || exit 2
# the suites build their own sandboxes: a run from inside a dispatched session (a build row, a reviewer)
# must not hand them that session's identity or rig (ORCH_ROW, ORCH_INSTANCE, … from launch.sh)
for _v in $(compgen -v ORCH_); do unset "$_v"; done
# the suites start tmux sessions: all of them on a private server, never the user's (a real rig lives there)
export TMUX_TMPDIR="$(mktemp -d "${TMPDIR:-/tmp}/otm-XXXXXX")"
export ORCH_TEST_TMUX_DIR="$TMUX_TMPDIR"
unset TMUX ORCH_TMUX_SOCKET
trap 'rm -rf "$TMUX_TMPDIR"' EXIT
start_sha=$(git rev-parse HEAD)   # the commit these suites test; a commit made mid-run is not stamped
fail=0
run(){ echo; echo "=== $*"; "$@" || fail=1; }
run python3 -m unittest tests/test_decisions.py tests/test_setup.py tests/test_pages.py
run python3 -m unittest tests/test_dashboard_modes.py tests/test_monitor_tui.py
run bash tests/test_mock_cycle.sh
run python3 -m unittest tests/test_v2_engine.py
run python3 -m unittest tests/test_v3_autonomy.py
run python3 -m unittest tests/test_v3_fallback.py tests/test_tmux_isolation.py
echo
if (( fail )); then echo "run_all: FAILED"; exit 1; fi
sha=$(git rev-parse HEAD)
if [[ $sha != "$start_sha" ]]; then
  echo "run_all: PASSED, but HEAD moved during the run (${start_sha:0:7} -> ${sha:0:7}) — not stamped (run again)"
elif [[ -z $(git status --porcelain | grep -v __pycache__ | grep -v '^?? .tested') ]]; then
  mkdir -p .tested && date '+%F %T' > ".tested/$sha"
  echo "run_all: PASSED — stamped .tested/${sha:0:7}"
else
  echo "run_all: PASSED on a dirty tree — not stamped (commit, then run again)"
fi
