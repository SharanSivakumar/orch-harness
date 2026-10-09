#!/usr/bin/env bash
# OpenCode adapter (scoped tier). Starts a visible OpenCode TUI and hands it the
# launch prompt with --prompt, which the TUI submits by itself once the model is
# ready — so there is no tmux paste and none of paste_kick's readiness probing.
#
# The launch prompt is the row's kick text (prompts/<id>.kick.txt) when the
# executor is listed in executors.arg_kick — a short pointer at the work order
# that can carry executor-specific binding lines (_KICK_OPENCODE.txt) — and the
# full work order otherwise. The kick is rewritten at every dispatch, so a kick
# left by an earlier executor of the same row is never what gets submitted.
#
# Model routing: orch.yaml `executors.models` is exported to this adapter as
#   ORCH_MODEL_OPENCODE          (key `opencode`: a model id, or provider/model)
#   ORCH_MODEL_OPENCODE_PROVIDER (key `opencode_provider`: an OpenCode provider id)
# and launched as `-m <provider>/<model>`. Pin both in the rig rather than
# inheriting the user's OpenCode default, so a later change of that default
# cannot silently move a rig's rows; a rig that sets neither gets OpenCode's
# own default. Per-row override: control/OPENCODE_MODEL_<id> (same forms).
#
# The session approves its own tool calls (--auto approves everything the
# OpenCode config does not explicitly deny), like the hermes --yolo and gemini
# rows. Deny rules, share and autoupdate belong in a rig-local OpenCode config,
# set by a launcher (executors.bin.opencode) that exports OPENCODE_CONFIG.
set -u
id=$1 x=$2 order=$3
bin="${ORCH_BIN_OPENCODE:-opencode}"
m=$(head -1 "$ORCH_INSTANCE/control/OPENCODE_MODEL_$id" 2>/dev/null | tr -d '[:space:]' || true)
mdl="${m:-${ORCH_MODEL_OPENCODE:-}}"
prov="${ORCH_MODEL_OPENCODE_PROVIDER:-}"
if [[ -n $mdl && $mdl != */* && -n $prov ]]; then mdl="$prov/$mdl"; fi
prompt=$order
kick="$ORCH_INSTANCE/prompts/$id.kick.txt"
if [[ " ${ORCH_ARG_KICK:-} " == *" $x "* && -s $kick ]]; then prompt=$kick; fi
flags="${ORCH_OPENCODE_FLAGS:---auto}"
# shellcheck disable=SC2086
if [[ -n $mdl ]]; then
  exec "$bin" -m "$mdl" $flags --prompt "$(cat "$prompt")"
fi
exec "$bin" $flags --prompt "$(cat "$prompt")"
