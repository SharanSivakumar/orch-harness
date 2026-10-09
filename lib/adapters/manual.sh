#!/usr/bin/env bash
# Manual adapter — a human operator's work order. Manual rows normally get no
# tmux window at all (the watcher announces them and waits); this exists so an
# explicit `launch` shows the order in a window instead of failing.
set -u
id=$1 model=$2 order=$3
echo "=== MANUAL WORK ORDER: $id ==="
echo
cat "$order"
echo
echo "----------------------------------------------------------------------"
echo "Close it with:  orch done $id \"summary\""
echo "Stop it with:   orch blocked $id \"reason\""
exec bash
