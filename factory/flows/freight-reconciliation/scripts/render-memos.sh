#!/usr/bin/env bash
# Re-check every batch and render one markdown memo per non-accept item, with a code-generated facts table.
set -euo pipefail
PYTHONPATH="$FLOWSTATE_VAR__flow_dir" exec python -m freight render-memos \
  --batches "$FLOWSTATE_VAR_memo_plan" --drafts-json "$FLOWSTATE_VAR_memo_draft" \
  --out-dir "$FLOWSTATE_VAR__run_artefact_dir/memos" --out "$FLOWSTATE_VAR_memos_index"
