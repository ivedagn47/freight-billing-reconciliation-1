#!/usr/bin/env bash
# Gate on each adjudication batch: every item decided once, with an offered disposition, existing clause
# ids and a justification whose figures all come from the packet (freight/adjudication.py).
set -euo pipefail
PYTHONPATH="$FLOWSTATE_VAR__flow_dir" exec python -m freight check-adjudications \
  --item "$FLOWSTATE_VAR_item" --decisions "$FLOWSTATE_VAR__run_artefact_dir/adjudications.json"
