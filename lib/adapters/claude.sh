#!/usr/bin/env bash
# Claude Code adapter — opus | sonnet | haiku. Visible TUI, never headless.
# The work order is passed as the initial prompt; the session writes its own
# sentinel as its final act.
#
# Adapter contract (all adapters): argv is <id> <executor> <work-order-path>,
# cwd is already the project root, and every ORCH_* setting is in the
# environment. The tmux window's life IS the session's life: we WAIT on the
# CLI (rather than `exec`) so an EXIT trap here can seal a session that ended
# cleanly without writing its sentinel. The pane still dies the moment the CLI
# exits, so the watcher's death-without-sentinel HALT remains for a session
# that dies having declared nothing.
set -u
id=$1 model=$2 order=$3
# A pinned model id (executors.models.<executor> in orch.yaml -> ORCH_MODEL_<EXECUTOR>)
# wins over the bare alias, so a CLI alias move cannot silently change a tier.
pin="ORCH_MODEL_${model^^}"
model="${!pin:-$model}"
flags="${ORCH_CLAUDE_FLAGS:---dangerously-skip-permissions}"
. "$(dirname "$(dirname "${BASH_SOURCE[0]}")")/closing.sh"
seal_on_exit(){
  seal_from_pane "$id" 2>/dev/null \
    && echo "[orch] sealed $id from its closing line (the status file was not written by the session)" >&2
}
trap seal_on_exit EXIT
# shellcheck disable=SC2086
"${ORCH_BIN_CLAUDE:-claude}" --model "$model" $flags "$(cat "$order")"
