#!/usr/bin/env bash
# smoke-fanout report (after the join): reads the merged stamp list and the folded summary.
# Inputs:  $FLOWSTATE_VAR_stamp           JSON list of stamp paths, in branch order ([] if no items)
#          $FLOWSTATE_VAR_fanout_summary  JSON object folded by the reducer ({} if no items)
# Output:  $FLOWSTATE_VAR_report
set -euo pipefail

ITEMS=$(jq -r '.[]' <<<"${FLOWSTATE_VAR_stamp:?}" | while read -r stamp; do jq -c '.item' "$stamp"; done | jq -s -c '.')
jq -n --argjson items "$ITEMS" --argjson summary "${FLOWSTATE_VAR_fanout_summary:?}" \
  '{_session_id: "smoke-fanout-report", branch_count: ($items | length), items: $items,
    summary_count: (($summary.order // []) | length)}' > "${FLOWSTATE_VAR_report:?}"
