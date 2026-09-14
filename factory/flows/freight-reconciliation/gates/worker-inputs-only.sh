#!/usr/bin/env bash
# Gate on each agent branch: the worker's transcript shows it read only its assigned inputs and wrote only
# inside its branch workspace (freight/audit.py). Needs the fan-out item to list "inputs".
set -euo pipefail
PYTHONPATH="$FLOWSTATE_VAR__flow_dir" exec python -m freight audit-worker \
  --run-dir "$FLOWSTATE_RUN_DIR" --workspace "$FLOWSTATE_VAR__run_artefact_dir" --item "$FLOWSTATE_VAR_item"
