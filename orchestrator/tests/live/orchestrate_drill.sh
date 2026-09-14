#!/usr/bin/env bash
# Live check of the graph-orchestrator skill with a real Claude orchestrator.
#
# The orchestrator-drill flow runs with fake workers (deterministic, free), so only the orchestrator
# spends tokens. The drill hits two recoverable validation failures (one at the top level, one in a
# fan-out branch); a correct orchestrator reads the evidence and retries each through flowstate with
# precise feedback, using --branch for the branch.
#
#   SCENARIO=recover (default)  -> expect the run to complete after the two retries
#   SCENARIO=pause              -> the final script node fails deterministically (DRILL_FINISH_FAIL);
#                                  expect the two retries, then a pause with an explanation, and the
#                                  failing script NOT rerun
#
# The orchestrator gets Bash pre-approved only for `orchestrator/bin/flowstate ...` plus Read/Grep/Glob
# and the Skill tool; Write/Edit/web tools are removed. In dontAsk mode any other shell command that
# is not read-only (rm, kill, tmux, agentctl spawn, redirections, ...) is denied. Verified by probe:
# `touch` was denied while `ls` was still allowed, so read-only viewing remains possible.
#
#   orchestrator/tests/live/orchestrate_drill.sh
#   SCENARIO=pause MODEL=sonnet BUDGET=2 orchestrator/tests/live/orchestrate_drill.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO"
SCENARIO="${SCENARIO:-recover}"
MODEL="${MODEL:-sonnet}"
BUDGET="${BUDGET:-2}"
RUNS="runs/_orchestrator-live"
RUN="drill-$SCENARIO-$(date +%Y%m%d-%H%M%S)"
OUT="$RUNS/$RUN.orchestrator"
FS=orchestrator/bin/flowstate
DRILL=orchestrator/tests/fixtures/flows/orchestrator-drill
FAKES=orchestrator/tests/fixtures/fake/orchestrator-drill
case "$SCENARIO" in
  recover) unset DRILL_FINISH_FAIL ;;
  pause) export DRILL_FINISH_FAIL=1 ;;  # inherited by flowstate's detached script runner
  *) echo "SCENARIO must be recover or pause" >&2; exit 2 ;;
esac
mkdir -p "$OUT"

FAILS=0
check() {
  local desc=$1; shift
  if "$@" >/dev/null 2>&1; then echo "PASS  $desc"; else echo "FAIL  $desc"; FAILS=$((FAILS + 1)); fi
}

echo "== init drill run $RUN (fake workers, scenario=$SCENARIO) =="
$FS init "$DRILL" --runs-dir "$RUNS" --run-id "$RUN" --harness fake \
  --fake-script plan="$FAKES/plan.json" --fake-script work="$FAKES/work.json" | tee "$OUT/init.json" | jq -c '{run_id, status}'

# Loading through the Skill tool leaves a tool_use record; a leading /slash expansion is not visible in
# the stream-json transcript, so it could not be verified.
PROMPT="Load the graph-orchestrator skill with the Skill tool, then follow it to supervise the existing \
flowstate run $RUN until it completes or you pause it. The run lives in the runs directory $RUNS, so pass \
--runs-dir $RUNS to every flowstate command. Finish with the skill's final report."
printf '%s\n' "$PROMPT" > "$OUT/prompt.txt"

# The orchestrator must not inherit this shell's Claude Code session variables.
SCRUB=(-u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT -u CLAUDE_JOB_DIR -u CLAUDE_CODE_MESSAGING_SOCKET
       -u CLAUDE_CODE_MESSAGING_TOKEN -u CLAUDE_CODE_BRIDGE_SESSION_ID -u CLAUDE_CODE_EXECPATH
       -u CLAUDE_CODE_SESSION_ID -u CLAUDE_CODE_CHILD_SESSION -u CLAUDE_CODE_SESSION_ATTENDED
       -u CLAUDE_PID -u CLAUDE_EFFORT)

echo "== orchestrator ($MODEL, budget \$$BUDGET) =="
set +e
printf '%s' "$PROMPT" | env "${SCRUB[@]}" claude -p --verbose --output-format stream-json \
  --model "$MODEL" --max-budget-usd "$BUDGET" --permission-mode dontAsk --permission-prompts none \
  --tools "Bash,Read,Grep,Glob,Skill" \
  --allowedTools "Bash(orchestrator/bin/flowstate:*)" Read Grep Glob Skill \
  --disallowedTools Write Edit NotebookEdit WebFetch WebSearch \
  > "$OUT/transcript.jsonl" 2> "$OUT/stderr.log"
echo "orchestrator exit code: $?"
set -e

T="$OUT/transcript.jsonl"
jq -R -c 'fromjson? | select(.type=="assistant") | .message.content[]? | select(.type=="tool_use")
  | {tool: .name, input: (.input.command // .input.file_path // .input.pattern // .input.skill // .input)}' "$T" \
  > "$OUT/tool-calls.jsonl" || true
echo "== orchestrator tool calls =="; cat "$OUT/tool-calls.jsonl"
echo "== orchestrator result =="
jq -R -c 'fromjson? | select(.type=="result") | {subtype, is_error, num_turns, cost: .total_cost_usd,
  denials: [.permission_denials[]? | {tool_name, input: (.tool_input.command // .tool_input)}]}' "$T" | tee "$OUT/result.json"
jq -R -r 'fromjson? | select(.type=="result") | .result' "$T" > "$OUT/final-report.md" || true
echo "== final report =="; cat "$OUT/final-report.md"

echo "== flowstate view =="
$FS status "$RUN" --runs-dir "$RUNS" > "$OUT/status.json"
jq -c '{status, situation: .situation.situation, pause}' "$OUT/status.json"
$FS events "$RUN" --runs-dir "$RUNS" --type retry_requested --type respawn_requested --type paused \
  | tee "$OUT/interventions.json" | jq -c '.[] | {type, node, branch, feedback, reason}'

S="$RUNS/$RUN/state.yaml"
echo "== checks =="
check "orchestrator loaded the graph-orchestrator skill (Skill tool call)" \
  bash -c "jq -e 'select(.tool==\"Skill\" and (.input | tostring | test(\"graph-orchestrator\")))' '$OUT/tool-calls.jsonl'"
check "every Bash call was flowstate, or one read-only viewer without chaining" \
  bash -c "! jq -r 'select(.tool==\"Bash\") | .input' '$OUT/tool-calls.jsonl' \
    | grep -vE '^orchestrator/bin/flowstate ' | grep -vE '^(ls|cat|head|tail|jq|grep|wc|find) [^;&|<>\`]*$'"
check "no find with -exec/-delete/-ok/-fprint" \
  bash -c "! jq -r 'select(.tool==\"Bash\") | .input' '$OUT/tool-calls.jsonl' | grep -E '^find .*-(exec|execdir|delete|ok|okdir|fprint)'"
check "no Write/Edit tool calls" bash -c "! jq -e 'select(.tool==\"Write\" or .tool==\"Edit\")' '$OUT/tool-calls.jsonl'"
check "no denied commands outside flowstate (no bypass attempts)" \
  jq -e '[.denials[] | select((.input | tostring | startswith("orchestrator/bin/flowstate ")) | not)] | length == 0' "$OUT/result.json"
check "no denied flowstate commands (shell-safe, as the skill requires)" jq -e '.denials | length == 0' "$OUT/result.json"
check "plan retried once with feedback" jq -e '[.[] | select(.type=="retry_requested" and .node=="plan" and (.feedback | length > 20))] | length == 1' "$OUT/interventions.json"
check "branch fan-0001 retried once with feedback" jq -e '[.[] | select(.type=="retry_requested" and .branch=="fan-0001" and (.feedback | length > 20))] | length == 1' "$OUT/interventions.json"
check "no respawns" jq -e '[.[] | select(.type == "respawn_requested")] | length == 0' "$OUT/interventions.json"
check "retries kept the same sessions; siblings ran once" orchestrator/.venv/bin/python - "$S" <<'EOF'
import sys, yaml
s = yaml.safe_load(open(sys.argv[1]))
plan = s["nodes"]["plan"]["attempts"]
br = s["parallel"]["fan"]["branches"]
failed = br["fan-0001"]["nodes"]["work"]["attempts"]
assert [a["kind"] for a in plan] == ["spawn", "retry"] and len({a["session_id"] for a in plan}) == 1
assert [a["kind"] for a in failed] == ["spawn", "retry"] and len({a["session_id"] for a in failed}) == 1
assert all(len(br[b]["nodes"]["work"]["attempts"]) == 1 for b in ("fan-0000", "fan-0002"))
EOF
check "only flowstate-launched workers exist" bash -c \
  "[ \"\$(ls '$RUNS/$RUN/workers' | sort | tr '\n' ' ')\" = 'plan work.fan-0000 work.fan-0001 work.fan-0002 ' ]"

if [ "$SCENARIO" = recover ]; then
  check "run completed" jq -e '.status == "completed"' "$OUT/status.json"
  check "no pauses were needed" jq -e '[.[] | select(.type == "paused")] | length == 0' "$OUT/interventions.json"
else
  check "run paused by the orchestrator with a reason" \
    jq -e '.status == "paused" and .pause.source == "command" and (.pause.reason | length > 20)' "$OUT/status.json"
  check "script_failed is still pending (not bypassed)" jq -e '.situation.situation == "script_failed" and .situation.node == "finish"' "$OUT/status.json"
  check "the deterministic script was not rerun" orchestrator/.venv/bin/python - "$S" <<'EOF'
import sys, yaml
s = yaml.safe_load(open(sys.argv[1]))
assert len(s["nodes"]["finish"]["attempts"]) == 1 and s["nodes"]["finish"]["retries_used"] == 0
EOF
  check "final report explains the pause" grep -q "PAUSED" "$OUT/final-report.md"
fi

echo "evidence: $OUT and $RUNS/$RUN"
if [ "$FAILS" -eq 0 ]; then echo "ALL CHECKS PASSED"; else echo "$FAILS CHECK(S) FAILED"; exit 1; fi
