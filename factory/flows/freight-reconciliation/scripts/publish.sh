#!/usr/bin/env bash
# Re-verify the report and the memos, then copy reconciliation-report.json and memos/ to publish_dir
# (freight/publish.py).
set -euo pipefail
source "$FLOWSTATE_VAR__flow_dir/scripts/_paths.sh"
PYTHONPATH="$FLOWSTATE_VAR__flow_dir" exec python -m freight publish \
  --report "$FLOWSTATE_VAR_report" --priced "$FLOWSTATE_VAR_priced" --manifest "$FLOWSTATE_VAR_manifest" \
  --memos-index "$FLOWSTATE_VAR_memos_index" --carriers "$(repo_path "$FLOWSTATE_VAR_carriers_config")" \
  --schema "$(repo_path "$FLOWSTATE_VAR_report_schema")" --dest "$(repo_path "$FLOWSTATE_VAR_publish_dir")" \
  --out "$FLOWSTATE_VAR_publication"
