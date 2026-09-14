#!/usr/bin/env bash
# Re-check every batch and merge: each open item decided exactly once across batches.
set -euo pipefail
PYTHONPATH="$FLOWSTATE_VAR__flow_dir" exec python -m freight merge-adjudications \
  --batches "$FLOWSTATE_VAR_adjudication_plan" --decisions-json "$FLOWSTATE_VAR_adjudication" \
  --priced "$FLOWSTATE_VAR_priced" --out "$FLOWSTATE_VAR_adjudications"
