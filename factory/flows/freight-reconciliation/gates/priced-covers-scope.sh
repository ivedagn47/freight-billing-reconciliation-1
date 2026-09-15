#!/usr/bin/env bash
# Gate after pricing: every in-scope line priced exactly once and every line and finding decided or
# offered for judgement (freight/coverage.py).
set -euo pipefail
PYTHONPATH="$FLOWSTATE_VAR__flow_dir" exec python -m freight check-priced \
  --manifest "$FLOWSTATE_VAR_manifest" --documents "$FLOWSTATE_VAR__run_artefact_dir/discovery/documents" \
  --priced "$FLOWSTATE_VAR_priced"
