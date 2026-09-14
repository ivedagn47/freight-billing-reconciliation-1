#!/usr/bin/env bash
# smoke-branch reducer for join j: folds each arriving branch into branch_summary.
# flowstate runs it once per branch, in branch order, as branches reach the join.
# Inputs:  $FLOWSTATE_REDUCER_SUMMARY_IN   JSON file: branch_summary so far ({} before the first branch)
#          $FLOWSTATE_BRANCH_ID            the branch being folded in
#          $FLOWSTATE_REDUCER_BRANCH_VARS  JSON file: the variables that branch produced
# Output:  $FLOWSTATE_REDUCER_SUMMARY_OUT  JSON object: the new branch_summary
set -euo pipefail

jq --arg branch "${FLOWSTATE_BRANCH_ID:?}" --slurpfile vars "${FLOWSTATE_REDUCER_BRANCH_VARS:?}" \
  '.arrivals = ((.arrivals // []) + [$branch])
   | .variables = ((.variables // {}) + $vars[0])
   | .count = (.arrivals | length)' \
  "${FLOWSTATE_REDUCER_SUMMARY_IN:?}" > "${FLOWSTATE_REDUCER_SUMMARY_OUT:?}"
