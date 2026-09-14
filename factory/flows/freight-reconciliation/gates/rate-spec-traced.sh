#!/usr/bin/env bash
# Gate on each extraction: the rate spec is schema-valid, names its contract, traces every number to a
# clause it cites, cites every clause, and uses only shipment vocabulary (freight/tracing.py).
set -euo pipefail
PYTHONPATH="$FLOWSTATE_VAR__flow_dir" exec python -m freight check-spec \
  --spec "$FLOWSTATE_VAR__run_artefact_dir/rate-spec.json" --item "$FLOWSTATE_VAR_item"
