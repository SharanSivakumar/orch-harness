#!/usr/bin/env bash
# Mock adapter — the dry-run executor used by tests/test_mock_cycle.sh.
# It occupies a real tmux window and writes a real sentinel, so it exercises
# the whole dispatch -> wait -> sentinel -> chain path with no model, no
# network and no spend. It is also the fastest way to prove a new ladder's
# dependency graph before any real executor is dispatched.
#
# Scripting a run (all optional, read at launch):
#   control/MOCK_<id>         line 1: CLOSED | BLOCKED | ESCALATED  (default CLOSED)
#                             line 2: the summary line
#   control/MOCK_DELAY_<id>   seconds to "work" before writing   (default 2)
#   control/MOCK_HANG_<id>    if present: never write a sentinel — exercises
#                             the stall warning and the died-without-sentinel
#                             halt.
set -u
id=$1 model=$2 order=$3
C="$ORCH_INSTANCE/control"
S="$ORCH_INSTANCE/state"
verdict=$(sed -n 1p "$C/MOCK_$id" 2>/dev/null || true)
summary=$(sed -n 2p "$C/MOCK_$id" 2>/dev/null || true)
: "${verdict:=CLOSED}"
: "${summary:=mock executor ran $id}"
delay=$(cat "$C/MOCK_DELAY_$id" 2>/dev/null || echo 2)

echo "[mock] row=$id executor=$model"
echo "[mock] work order: $order"
echo "[mock] verdict=$verdict delay=${delay}s"
sleep "$delay"

if [[ -f "$C/MOCK_HANG_$id" ]]; then
  echo "[mock] MOCK_HANG_$id present — hanging deliberately, no sentinel will be written."
  while true; do sleep 60; done
fi

printf '%s\n%s\n' "$verdict" "$summary" > "$S/$id.status"
echo "[mock] wrote $S/$id.status = $verdict"
sleep 1
