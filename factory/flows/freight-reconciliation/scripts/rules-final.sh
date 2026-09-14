#!/usr/bin/env bash
# Round 2 (if any) must agree copy against copy; adopted specs are cached. Exits 1 if any carrier has no
# agreed spec, so pricing never runs on a contract reading that two workers did not both produce.
set -euo pipefail
PYTHONPATH="$FLOWSTATE_VAR__flow_dir" exec python -m freight rules-final \
  --plan "$FLOWSTATE_VAR_rules_plan" --agreement "$FLOWSTATE_VAR_rules_agreement" --specs-json "$FLOWSTATE_VAR_rate_spec_rerun" \
  --out-dir "$FLOWSTATE_VAR__run_artefact_dir/rules" --out "$FLOWSTATE_VAR_rules_final" --run-id "$FLOWSTATE_VAR__run_id"
