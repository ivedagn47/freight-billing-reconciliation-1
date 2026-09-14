#!/usr/bin/env bash
# Round 1: trace both copies per carrier, compare them by behaviour, adopt or assign a second round.
# Exits 1 (the run stops for a human) on invalid copies or unrepresentable contract terms.
set -euo pipefail
PYTHONPATH="$FLOWSTATE_VAR__flow_dir" exec python -m freight rules-agree \
  --plan "$FLOWSTATE_VAR_rules_plan" --specs-json "$FLOWSTATE_VAR_rate_spec" \
  --out-dir "$FLOWSTATE_VAR__run_artefact_dir/rules" --out "$FLOWSTATE_VAR_rules_agreement"
