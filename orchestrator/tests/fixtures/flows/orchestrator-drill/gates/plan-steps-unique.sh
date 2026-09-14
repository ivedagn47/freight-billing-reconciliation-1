#!/usr/bin/env bash
# Gate (plan -> fan): every step name in the plan must be different.
set -euo pipefail

jq -e '(.steps | unique | length) == (.steps | length)' "${FLOWSTATE_VAR_plan:?}" > /dev/null \
  || { echo "plan has duplicate step names: $(jq -c .steps "$FLOWSTATE_VAR_plan")" >&2; exit 1; }
