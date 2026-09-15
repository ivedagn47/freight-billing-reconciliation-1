#!/usr/bin/env bash
# One facts entry per non-accept report row, batched into packets (freight/memos.py).
set -euo pipefail
source "$FLOWSTATE_VAR__flow_dir/scripts/_paths.sh"
PYTHONPATH="$FLOWSTATE_VAR__flow_dir" exec python -m freight memo-plan \
  --priced "$FLOWSTATE_VAR_priced" --report "$FLOWSTATE_VAR_report" --clauses-dir "$FLOWSTATE_VAR_clauses_dir" \
  --carriers "$(repo_path "$FLOWSTATE_VAR_carriers_config")" --batch-size "$FLOWSTATE_VAR_memo_batch_size" \
  --out-dir "$FLOWSTATE_VAR__run_artefact_dir/memo-work" --out "$FLOWSTATE_VAR_memo_plan"
