#!/usr/bin/env bash
# Clause indexes, shipment vocabulary, cache lookups and extraction assignments (freight/rules.py).
# Carriers come from the manifest when one is given, else from carrier_ids.
set -euo pipefail
source "$FLOWSTATE_VAR__flow_dir/scripts/_paths.sh"
PYTHONPATH="$FLOWSTATE_VAR__flow_dir" exec python -m freight rules-plan \
  --carriers "$(repo_path "$FLOWSTATE_VAR_carriers_config")" --root "$(repo_path "$FLOWSTATE_VAR_data_root")" \
  --shipments "$(repo_path "$FLOWSTATE_VAR_shipments_file")" \
  --manifest "$(repo_path "$FLOWSTATE_VAR_manifest")" --carrier-ids "$FLOWSTATE_VAR_carrier_ids" \
  --cache-dir "$(repo_path "$FLOWSTATE_VAR_rules_cache_dir")" --use-cache "$FLOWSTATE_VAR_use_rules_cache" \
  --out-dir "$FLOWSTATE_VAR__run_artefact_dir/rules" --out "$FLOWSTATE_VAR_rules_plan"
