#!/usr/bin/env bash
# Parse every invoice document and decide the run's scope for the period (freight/documents.py).
set -euo pipefail
source "$FLOWSTATE_VAR__flow_dir/scripts/_paths.sh"
PYTHONPATH="$FLOWSTATE_VAR__flow_dir" exec python -m freight discover \
  --invoices "$(repo_path "$FLOWSTATE_VAR_invoices_dir")" --carriers "$(repo_path "$FLOWSTATE_VAR_carriers_config")" \
  --period "$FLOWSTATE_VAR_period" --documents "$FLOWSTATE_VAR__run_artefact_dir/discovery/documents" \
  --out "$FLOWSTATE_VAR_manifest"
