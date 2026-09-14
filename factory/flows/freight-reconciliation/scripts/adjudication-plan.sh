#!/usr/bin/env bash
# Batch the items policy left open into facts packets for adjudication (freight/adjudication.py).
set -euo pipefail
PYTHONPATH="$FLOWSTATE_VAR__flow_dir" exec python -m freight adjudication-plan \
  --priced "$FLOWSTATE_VAR_priced" --clauses-dir "$FLOWSTATE_VAR_clauses_dir" --batch-size "$FLOWSTATE_VAR_adjudication_batch_size" \
  --out-dir "$FLOWSTATE_VAR__run_artefact_dir/adjudication" --out "$FLOWSTATE_VAR_adjudication_plan"
