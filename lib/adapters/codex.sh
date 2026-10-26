#!/usr/bin/env bash
# Codex CLI adapter. Visible TUI. Writes its own sentinel.
set -u
id=$1 model=$2 order=$3
flags="${ORCH_CODEX_FLAGS:---dangerously-bypass-approvals-and-sandbox}"
# shellcheck disable=SC2086
eval "extra=(${ORCH_EXTRA_ARGS:-})"
# shellcheck disable=SC2086
exec "${ORCH_BIN_CODEX:-codex}" $flags "${extra[@]}" "$(cat "$order")"
