#!/usr/bin/env bash
# Drill finish (after the join): counts the merged step reports.
# Inputs:  $FLOWSTATE_VAR_report   JSON list of report paths, in branch order
# Output:  $FLOWSTATE_VAR_summary
set -euo pipefail

[ -z "${DRILL_FINISH_FAIL:-}" ] || { echo "cannot count reports: report index is missing" >&2; exit 3; }

jq -n --argjson reports "${FLOWSTATE_VAR_report:?}" \
  '{_session_id: "orchestrator-drill-finish", reports: ($reports | length)}' > "${FLOWSTATE_VAR_summary:?}"
