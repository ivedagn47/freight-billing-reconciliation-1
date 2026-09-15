#!/usr/bin/env bash
# Price every in-scope line from shipment records and the adopted rate specs, then apply the disposition
# policy (freight/pricing.py, freight/policy.py).
set -euo pipefail
source "$FLOWSTATE_VAR__flow_dir/scripts/_paths.sh"
PYTHONPATH="$FLOWSTATE_VAR__flow_dir" exec python -m freight price \
  --manifest "$FLOWSTATE_VAR_manifest" --documents "$FLOWSTATE_VAR__run_artefact_dir/discovery/documents" \
  --shipments "$(repo_path "$FLOWSTATE_VAR_shipments_file")" --specs-json "$FLOWSTATE_VAR_rate_specs" \
  --policy "$FLOWSTATE_VAR__flow_dir/config/policy.yml" --out "$FLOWSTATE_VAR_priced"
