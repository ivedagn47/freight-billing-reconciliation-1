#!/usr/bin/env bash
# Gate after discovery: no document whose scope could not be established, and something in scope
# (freight/coverage.py). A failure needs a human: the documents or the carrier config must change.
set -euo pipefail
PYTHONPATH="$FLOWSTATE_VAR__flow_dir" exec python -m freight check-scope --manifest "$FLOWSTATE_VAR_manifest"
