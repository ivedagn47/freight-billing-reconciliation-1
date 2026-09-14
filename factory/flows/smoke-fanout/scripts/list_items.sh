#!/usr/bin/env bash
# smoke-fanout list_items: turns the comma-separated item names into a JSON list.
# Inputs:  $FLOWSTATE_VAR_item_names       e.g. "alpha,beta,gamma" ("" gives an empty list)
# Output:  $FLOWSTATE_VAR__run_artefact_dir/items.json  ({"items": [...]}; the items variable is
#          bound from /items by flowstate)
set -euo pipefail

OUT="${FLOWSTATE_VAR__run_artefact_dir:?}/items.json"
jq -n --arg names "${FLOWSTATE_VAR_item_names-}" \
  '{_session_id: "smoke-fanout-list",
    items: ($names | split(",") | map(gsub("^\\s+|\\s+$"; "")) | map(select(length > 0)))}' > "$OUT"
