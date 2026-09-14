#!/usr/bin/env bash
# Gate (inside each branch): the description must name this branch's item and branch id.
set -euo pipefail

jq -e --arg item "${FLOWSTATE_VAR_item:?}" --arg branch "${FLOWSTATE_VAR__branch_id:?}" \
  '.item == $item and .branch_id == $branch' "${FLOWSTATE_VAR_description:?}" > /dev/null \
  || { echo "description does not match item '${FLOWSTATE_VAR_item}' / branch '${FLOWSTATE_VAR__branch_id}'" >&2; exit 1; }
