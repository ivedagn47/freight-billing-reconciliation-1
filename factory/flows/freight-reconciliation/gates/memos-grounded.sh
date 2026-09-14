#!/usr/bin/env bash
# Gate on each memo batch: every memo drafted once, every field present and within length, every figure
# copied from that memo's facts (freight/memos.py).
set -euo pipefail
PYTHONPATH="$FLOWSTATE_VAR__flow_dir" exec python -m freight check-memos \
  --item "$FLOWSTATE_VAR_item" --drafts "$FLOWSTATE_VAR__run_artefact_dir/memo-drafts.json"
