#!/usr/bin/env bash
# OpenCode adapter (junior tier). Starts a visible OpenCode TUI and hands it the
# launch prompt with --prompt, which the TUI submits by itself once the model is
# ready — so there is no tmux paste and none of paste_kick's readiness probing.
#
# The launch prompt is the row's kick text (prompts/<id>.kick.txt) when the
# executor is listed in executors.arg_kick — a short pointer at the work order
# that can carry executor-specific binding lines (_KICK_OPENCODE.txt) — and the
# full work order otherwise. The kick is rewritten at every dispatch, so a kick
# left by an earlier executor of the same row is never what gets submitted.
#
# Model: never chosen here for the base executor. OpenCode runs its own configured default
# model (the operator's OpenCode config) — no -m, so executors.models.opencode and
# executors.models.opencode_provider are ignored (orch lint warns), and so is any
# control/OPENCODE_MODEL_<id> file. A rig-local OPENCODE_CONFIG must not set "model".
# The TUI has no --model flag (only `opencode run` has; measured: the TUI prints its
# help and exits). An executor VARIANT (executors.variants.<name>.model: provider/model) sets
# ORCH_VARIANT_MODEL, and this adapter passes it as OPENCODE_CONFIG_CONTENT={"model": …}, the
# inline config OpenCode layers over every config file (measured: the TUI then shows that model).
# An OPENCODE_CONFIG_CONTENT already in the environment (a variant's env) is left alone.
#
# The session approves its own tool calls (--auto approves everything the
# OpenCode config does not explicitly deny), like the gemini
# rows. Deny rules, share and autoupdate belong in a rig-local OpenCode config,
# set by a launcher (executors.bin.opencode) that exports OPENCODE_CONFIG.
set -u
id=$1 x=$2 order=$3
bin="${ORCH_BIN_OPENCODE:-opencode}"
prompt=$order
kick="$ORCH_INSTANCE/prompts/$id.kick.txt"
if [[ " ${ORCH_ARG_KICK:-} " == *" $x "* && -s $kick ]]; then prompt=$kick; fi
flags="${ORCH_OPENCODE_FLAGS:---auto}"
if [[ -n ${ORCH_VARIANT_MODEL:-} && -z ${OPENCODE_CONFIG_CONTENT:-} ]]; then
  OPENCODE_CONFIG_CONTENT=$(python3 -c 'import json,sys; print(json.dumps({"model": sys.argv[1]}))' "$ORCH_VARIANT_MODEL") \
    || { echo "[orch] cannot build the variant model config" >&2; exit 2; }
  export OPENCODE_CONFIG_CONTENT
fi
eval "extra=(${ORCH_EXTRA_ARGS:-})"
# shellcheck disable=SC2086
exec "$bin" $flags "${extra[@]}" --prompt "$(cat "$prompt")"
