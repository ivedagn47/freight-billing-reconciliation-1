#!/usr/bin/env bash
# smoke-fanout reducer for join collect: folds each branch into fanout_summary, in branch order.
# Inputs:  $FLOWSTATE_REDUCER_SUMMARY_IN   JSON file: fanout_summary so far ({} before the first branch)
#          $FLOWSTATE_BRANCH_ID, $FLOWSTATE_VAR_item, $FLOWSTATE_VAR_stamp (this branch's scope)
# Output:  $FLOWSTATE_REDUCER_SUMMARY_OUT  JSON object: the new fanout_summary
set -euo pipefail

WORDS=$(jq '.words' "${FLOWSTATE_VAR_stamp:?}")
jq --arg branch "${FLOWSTATE_BRANCH_ID:?}" --arg item "${FLOWSTATE_VAR_item:?}" --argjson words "$WORDS" \
  '.order = ((.order // []) + [$branch])
   | .items = ((.items // {}) + {($branch): $item})
   | .total_words = ((.total_words // 0) + $words)' \
  "${FLOWSTATE_REDUCER_SUMMARY_IN:?}" > "${FLOWSTATE_REDUCER_SUMMARY_OUT:?}"
