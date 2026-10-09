#!/usr/bin/env bash
# Hermes PRO adapter — lead tier on a larger model, same kick-paste mechanic
# as hermes.sh. The provider MUST be named explicitly: with only -m, the model
# resolves against the default provider and the session dies on an HTTP 422
#.
#
# Configure in orch.yaml:
#   executors.models.hermes_pro:          the model id
#   executors.models.hermes_provider:     the provider to route it through
set -u
id=$1 model=$2 order=$3
bin="${ORCH_BIN_HERMES:-hermes}"
mdl="${ORCH_MODEL_HERMES_PRO:-deepseek-v4-pro}"
prov="${ORCH_MODEL_HERMES_PROVIDER:-deepseek}"
exec "$bin" -m "$mdl" --provider "$prov"
