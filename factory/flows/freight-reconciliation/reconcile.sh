#!/usr/bin/env bash
# Run the freight reconciliation headless: a Claude orchestrator session loads the reconcile-freight skill
# (which loads graph-orchestrator) and supervises one flowstate run of freight-reconciliation. Workers are
# launched only by flowstate; the orchestrator can only run flowstate commands and read files.
#
#   factory/flows/freight-reconciliation/reconcile.sh 2026-07          # publishes to the repository root
#   PUBLISH_DIR=runs/_dry/published RUNS_DIR=runs/_dry factory/flows/freight-reconciliation/reconcile.sh 2026-07
#   MODEL=opus BUDGET=5 FRESH_EXTRACTION=1 RUN_ID=freight-2026-07-a factory/flows/freight-reconciliation/reconcile.sh 2026-07
#
# In an interactive Claude Code session the same thing is: /reconcile-freight 2026-07
#
# Evidence: the run directory <RUNS_DIR>/<RUN_ID>/ (state, events, artefacts, workers, logs) and the
# orchestrator's own session in <RUNS_DIR>/<RUN_ID>.orchestrator/ (prompt, transcript, tool calls, result).
set -euo pipefail

PERIOD="${1:?usage: reconcile.sh YYYY-MM}"
[[ "$PERIOD" =~ ^[0-9]{4}-[0-9]{2}$ ]] || { echo "period must be YYYY-MM" >&2; exit 2; }
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO"
MODEL="${MODEL:-sonnet}"
BUDGET="${BUDGET:-5}"
RUNS_DIR="${RUNS_DIR:-runs}"
RUN_ID="${RUN_ID:-freight-$PERIOD-$(date -u +%Y%m%d-%H%M%S)}"
OUT="$RUNS_DIR/$RUN_ID.orchestrator"
FS=orchestrator/bin/flowstate
mkdir -p "$OUT"

PROMPT="Load the reconcile-freight skill with the Skill tool and follow it to reconcile billing period $PERIOD. \
Use run id $RUN_ID and runs directory $RUNS_DIR, so pass --runs-dir $RUNS_DIR to every flowstate command."
if [ -n "${PUBLISH_DIR:-}" ]; then PROMPT="$PROMPT Publish to $PUBLISH_DIR (set publish_dir)."; fi
if [ -n "${FRESH_EXTRACTION:-}" ]; then PROMPT="$PROMPT Extract every contract afresh (set use_rules_cache=false)."; fi
PROMPT="$PROMPT Finish with the skill's final report."
printf '%s\n' "$PROMPT" > "$OUT/prompt.txt"

# The orchestrator must not inherit this shell's Claude Code session variables.
SCRUB=(-u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT -u CLAUDE_JOB_DIR -u CLAUDE_CODE_MESSAGING_SOCKET
       -u CLAUDE_CODE_MESSAGING_TOKEN -u CLAUDE_CODE_BRIDGE_SESSION_ID -u CLAUDE_CODE_EXECPATH
       -u CLAUDE_CODE_SESSION_ID -u CLAUDE_CODE_CHILD_SESSION -u CLAUDE_CODE_SESSION_ATTENDED
       -u CLAUDE_PID -u CLAUDE_EFFORT)

echo "== orchestrator ($MODEL, budget \$$BUDGET): run $RUN_ID in $RUNS_DIR =="
set +e
printf '%s' "$PROMPT" | env "${SCRUB[@]}" claude -p --verbose --output-format stream-json \
  --model "$MODEL" --max-budget-usd "$BUDGET" --permission-mode dontAsk --permission-prompts none \
  --tools "Bash,Read,Grep,Glob,Skill" \
  --allowedTools "Bash(orchestrator/bin/flowstate:*)" Read Grep Glob Skill \
  --disallowedTools Write Edit NotebookEdit WebFetch WebSearch \
  > "$OUT/transcript.jsonl" 2> "$OUT/stderr.log"
CODE=$?
set -e
echo "orchestrator exit code: $CODE"

T="$OUT/transcript.jsonl"
jq -R -c 'fromjson? | select(.type=="assistant") | .message.content[]? | select(.type=="tool_use")
  | {tool: .name, input: (.input.command // .input.file_path // .input.pattern // .input.skill // .input)}' "$T" \
  > "$OUT/tool-calls.jsonl" || true
jq -R -c 'fromjson? | select(.type=="result") | {subtype, is_error, num_turns, cost: .total_cost_usd,
  denials: [.permission_denials[]? | {tool_name, input: (.tool_input.command // .tool_input)}]}' "$T" > "$OUT/result.json" || true
jq -R -r 'fromjson? | select(.type=="result") | .result' "$T" > "$OUT/final-report.md" || true

echo "== final report =="; cat "$OUT/final-report.md"
echo "== orchestrator result =="; cat "$OUT/result.json"
if [ -f "$RUNS_DIR/$RUN_ID/state.yaml" ]; then
  $FS status "$RUN_ID" --runs-dir "$RUNS_DIR" > "$OUT/status.json"
  jq -c '{status, situation: .situation.situation, pause}' "$OUT/status.json"
  $FS events "$RUN_ID" --runs-dir "$RUNS_DIR" --type retry_requested --type respawn_requested --type paused \
    > "$OUT/interventions.json"
  jq -c '.[] | {type, node, branch, feedback, reason}' "$OUT/interventions.json"
fi
RUN_STATUS="$(jq -r .status "$OUT/status.json" 2>/dev/null || echo unknown)"
if [ "$CODE" -ne 0 ] && [ "$RUN_STATUS" = completed ]; then
  echo "NOTE: the run completed, but the orchestrator session ended with an error before finishing its report"
  echo "      (see $OUT/final-report.md). What was published is recorded in $RUNS_DIR/$RUN_ID/artefacts/publication.json."
fi
echo "evidence: $RUNS_DIR/$RUN_ID and $OUT"
exit "$CODE"
