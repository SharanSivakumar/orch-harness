#!/usr/bin/env bash
# Hermes-Flash (hermesflash) adapter — the lighter scoped tier: a Hermes session
# on the profile's default model (DeepSeek V4 Flash). Same kick-paste mechanic
# and launcher as hermes.sh (the dispatcher pastes prompts/<id>.kick.txt once
# the prompt is ready; ORCH_BIN_HERMES is shared, so a rig-local launcher such
# as a custom launcher script applies to both tiers). Per-row model override in
# control/HERMES_MODEL_<id>.
#
# The model and provider are pinned explicitly rather than inherited from the
# user's default profile, so a later change of that default cannot silently
# move a rig's rows; the provider MUST be named (with only -m the model
# resolves against the default provider — a lesson learned the hard way).
#
# Configure in orch.yaml:
#   executors.models.hermesflash:          the model id (deepseek-v4-flash-0731)
#   executors.models.hermesflash_provider: the provider name (scaleway)
set -u
id=$1 model=$2 order=$3
bin="${ORCH_BIN_HERMES:-hermes}"
mdl="${ORCH_MODEL_HERMESFLASH:-deepseek-v4-flash-0731}"
prov="${ORCH_MODEL_HERMESFLASH_PROVIDER:-scaleway}"
m=$(cat "$ORCH_INSTANCE/control/HERMES_MODEL_$id" 2>/dev/null || true)
exec "$bin" -m "${m:-$mdl}" --provider "$prov"
