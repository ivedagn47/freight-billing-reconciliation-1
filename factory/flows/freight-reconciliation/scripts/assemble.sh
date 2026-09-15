#!/usr/bin/env bash
# Assemble the report from pricing, policy and adjudications; verify it against report.schema.json and
# every invariant, including one row per in-scope line (freight/report.py).
set -euo pipefail
source "$FLOWSTATE_VAR__flow_dir/scripts/_paths.sh"
PYTHONPATH="$FLOWSTATE_VAR__flow_dir" exec python -m freight assemble \
  --priced "$FLOWSTATE_VAR_priced" --policy "$FLOWSTATE_VAR__flow_dir/config/policy.yml" \
  --schema "$(repo_path "$FLOWSTATE_VAR_report_schema")" --adjudications "$FLOWSTATE_VAR_adjudications" \
  --manifest "$FLOWSTATE_VAR_manifest" --out "$FLOWSTATE_VAR_report"
