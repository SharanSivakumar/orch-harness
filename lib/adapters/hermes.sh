#!/usr/bin/env bash
# Hermes adapter (scoped tier). Starts a BARE interactive Hermes TUI; the
# dispatcher pastes prompts/<id>.kick.txt in once the prompt has drawn
# (paste_kick in orchestrator_core.sh), which avoids every shell-quoting
# hazard of a long argv — the p2p3-batch pattern.
#
# A per-row model override lives in control/HERMES_MODEL_<id>, e.g. to route
# one row through a different provider's API for the same tier.
#
# Per-rig routing: orch.yaml `executors.models` is exported to this adapter as
#   ORCH_MODEL_HERMES          (key `hermes`)
#   ORCH_MODEL_HERMES_PROVIDER (key `hermes_provider`)
# When BOTH are set, launch `hermes -m <model> --provider <provider>` (the
# provider MUST be named — with only -m the model resolves against the default
# provider and dies on an HTTP 4xx). A rig
# that sets neither keeps the original bare-`hermes` behaviour (default
# profile → default provider), so rigs with `models: {}` are unchanged.
set -u
id=$1 model=$2 order=$3
bin="${ORCH_BIN_HERMES:-hermes}"
m=$(cat "$ORCH_INSTANCE/control/HERMES_MODEL_$id" 2>/dev/null || true)
mdl="${m:-${ORCH_MODEL_HERMES:-}}"
prov="${ORCH_MODEL_HERMES_PROVIDER:-}"
if [[ -n ${mdl} && -n ${prov} ]]; then
  exec "$bin" -m "$mdl" --provider "$prov"
fi
if [[ -n ${mdl} ]]; then
  exec "$bin" -m "$mdl"
fi
exec "$bin"
