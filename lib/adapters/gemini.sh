#!/usr/bin/env bash
# Gemini / Antigravity (agy) adapter. Visible TUI, work order passed with -i.
# Per-row model override: control/GEMINI_MODEL_<id>.
set -u
id=$1 model=$2 order=$3
gm=$(cat "$ORCH_INSTANCE/control/GEMINI_MODEL_$id" 2>/dev/null || true)
: "${gm:=${ORCH_MODEL_GEMINI:-gemini-3.7-flash-high}}"
flags="${ORCH_GEMINI_FLAGS:---dangerously-skip-permissions}"
# shellcheck disable=SC2086
eval "extra=(${ORCH_EXTRA_ARGS:-})"
# shellcheck disable=SC2086
exec "${ORCH_BIN_GEMINI:-agy}" --model "$gm" $flags "${extra[@]}" -i "$(cat "$order")"
