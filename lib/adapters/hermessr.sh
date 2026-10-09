#!/usr/bin/env bash
# Hermes-Sr (hermessr) adapter — Codex-equivalent autonomy tier running a Hermes
# session on OpenRouter + GLM 5.3 Flash.  Same kick-paste mechanic as hermes.sh
# (the dispatcher pastes prompts/<id>.kick.txt once the TUI has drawn), with a
# per-row model override in control/HERMES_MODEL_<id>.
#
# The provider MUST be named explicitly: with only -m, the model resolves
# against the default provider and the session dies on an HTTP 422 (the
# (a known pitfall).
#
# Configure in orch.yaml:
#   executors.models.hermes_sr:          the OpenRouter model id (z-ai/glm-5.3-flash)
#   executors.models.hermes_sr_provider: the provider name (openrouter)
set -u
id=$1 model=$2 order=$3
bin="${ORCH_BIN_HERMES:-hermes}"
mdl="${ORCH_MODEL_HERMES_SR:-z-ai/glm-5.3-flash}"
prov="${ORCH_MODEL_HERMES_SR_PROVIDER:-openrouter}"
m=$(cat "$ORCH_INSTANCE/control/HERMES_MODEL_$id" 2>/dev/null || true)
if [[ -n ${m:-} ]]; then
  exec "$bin" -m "$m" --provider "$prov"
fi
exec "$bin" -m "$mdl" --provider "$prov"
