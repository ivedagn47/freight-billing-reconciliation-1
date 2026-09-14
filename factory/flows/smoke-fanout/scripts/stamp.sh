#!/usr/bin/env bash
# smoke-fanout stamp (runs inside each branch): records the branch, its item and the word count.
# Inputs:  $FLOWSTATE_VAR_description  this branch's description JSON
#          $FLOWSTATE_VAR_item, $FLOWSTATE_VAR__branch_id
# Output:  $FLOWSTATE_VAR_stamp         (a path inside this branch's artefact directory)
set -euo pipefail

jq --arg branch "${FLOWSTATE_VAR__branch_id:?}" --arg item "${FLOWSTATE_VAR_item:?}" \
  '{_session_id: "smoke-fanout-stamp", branch_id: $branch, item: $item,
    words: (.text | split(" ") | map(select(length > 0)) | length)}' \
  "${FLOWSTATE_VAR_description:?}" > "${FLOWSTATE_VAR_stamp:?}"
