#!/usr/bin/env bash
# Live end-to-end check of agentctl with the real Claude harness, through tmux.
# spawn -> wait -> send (resume) -> wait -> status/logs -> kill, plus isolation checks.
# Spends a few cents (haiku by default). Evidence lands in runs/_smoke-claude-<ts>/.
#
#   orchestrator/tests/live/smoke_claude_worker.sh
#   MODEL=sonnet PERMISSION_MODE=acceptEdits orchestrator/tests/live/smoke_claude_worker.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
RUN="$REPO/runs/_smoke-claude-$(date +%Y%m%d-%H%M%S)"
WORK="$RUN/work"
MODEL="${MODEL:-haiku}"
PERMISSION_MODE="${PERMISSION_MODE:-auto}"
AC=("$REPO/orchestrator/bin/agentctl" --registry "$RUN/workers")
mkdir -p "$WORK"

FAILS=0
step() { printf '\n== %s ==\n' "$*"; }
check() {  # check <description> <command...>
  local desc=$1; shift
  if "$@" >/dev/null 2>&1; then echo "PASS  $desc"; else echo "FAIL  $desc"; FAILS=$((FAILS + 1)); fi
}

cat > "$RUN/prompt.md" <<'EOF'
You are a trivial smoke-test worker.
1. Use the Write tool to create hello.json in your current working directory containing exactly {"greeting": "hello"}
2. Try to run the shell command `touch bash-was-here.txt` with a Bash tool, and try to fetch https://example.com with a web tool. If those tools are not available to you, say so and do not work around it.
3. End with one line: DONE, followed by the names of the tools you have.
EOF

step "spawn w1 (model=$MODEL, permission-mode=$PERMISSION_MODE)"
"${AC[@]}" spawn w1 --harness claude --cwd "$WORK" --prompt-file "$RUN/prompt.md" \
  --model "$MODEL" --permission-mode "$PERMISSION_MODE" --max-budget-usd 0.50 \
  --stall-after 180 --add-dir "$REPO/data" | tee "$RUN/w1-spawn.json" | jq '{state, session_id, tmux_session}'
SID=$(jq -r .session_id "$RUN/w1-spawn.json")
TMUX_NAME=$(jq -r .tmux_session "$RUN/w1-spawn.json")
check "tmux session $TMUX_NAME exists while running" tmux has-session -t "=$TMUX_NAME"

step "wait w1"
"${AC[@]}" wait w1 --timeout 300 | tee "$RUN/w1-wait1.json" \
  | jq '{outcome, exit_code, cost_usd, num_turns, last_result: .last_result | {subtype, is_error, permission_denials, text}}'
check "first invocation exited cleanly" jq -e '.outcome == "exited"' "$RUN/w1-wait1.json"
check "worker wrote hello.json via Write tool" jq -e '.greeting == "hello"' "$WORK/hello.json"
check "shell command did not run (no bash-was-here.txt)" test ! -e "$WORK/bash-was-here.txt"
check "cost and turn count recorded" jq -e '.cost_usd > 0 and .num_turns >= 1' "$RUN/w1-wait1.json"

T0="$RUN/workers/w1/invocations/000/transcript.jsonl"
step "isolation evidence from the worker's own init event"
jq -c 'select(.type=="system" and .subtype=="init") | {session_id, tools, skills, slash_commands, mcp_servers, plugins, memory_paths}' "$T0"
check "session id is the one agentctl assigned" jq -e --arg s "$SID" 'select(.type=="system" and .subtype=="init") | .session_id == $s' "$T0"
check "only file tools exist" jq -e 'select(.type=="system" and .subtype=="init") | (.tools | sort) == ["Edit","Glob","Grep","Read","Write"]' "$T0"
check "no skills or slash commands loaded" jq -e 'select(.type=="system" and .subtype=="init") | (.skills | length) == 0 and (.slash_commands | length) == 0' "$T0"
check "no MCP servers, no auto-memory" jq -e 'select(.type=="system" and .subtype=="init") | (.mcp_servers | length) == 0 and .memory_paths == null' "$T0"
step "tools the worker actually called"
jq -r 'select(.type=="assistant") | .message.content[]? | select(.type=="tool_use") | .name' "$T0" | sort | uniq -c
check "no Bash/WebFetch/WebSearch tool calls" bash -c "! jq -r 'select(.type==\"assistant\") | .message.content[]? | select(.type==\"tool_use\") | .name' '$T0' | grep -qE '^(Bash|WebFetch|WebSearch)$'"

step "send w1 (resume same session)"
"${AC[@]}" send w1 --message "In one line: what file did you create earlier and what did it contain?" | jq '{state, invocations}'
"${AC[@]}" wait w1 --timeout 300 | tee "$RUN/w1-wait2.json" | jq '{outcome, invocations, cost_usd, num_turns, per_invocation, answer: .last_result.text}'
check "second invocation exited cleanly" jq -e '.outcome == "exited" and .invocations == 2' "$RUN/w1-wait2.json"
check "both invocations share one session id" jq -e --arg s "$SID" '[.per_invocation[].session_id] == [$s, $s]' "$RUN/w1-wait2.json"
check "resumed worker remembers hello.json" jq -e '.last_result.text | test("hello")' "$RUN/w1-wait2.json"
check "tmux session closed after exit" bash -c "! tmux has-session -t '=$TMUX_NAME'"

step "status w1"
"${AC[@]}" status w1 | jq '{state, invocations, exit_code, cost_usd, num_turns, paths}'
step "logs w1 (text, last 12 lines)"
"${AC[@]}" logs w1 --tail 12
step "logs w1 (stderr)"
"${AC[@]}" logs w1 --stream stderr | tail -5

step "kill: spawn w2 on a long task, then kill it mid-run"
"${AC[@]}" spawn w2 --harness claude --cwd "$WORK" --model "$MODEL" --permission-mode "$PERMISSION_MODE" \
  --max-budget-usd 0.20 --prompt "Write a 3000-word story about a lighthouse, directly in your reply." | jq '{state}'
CHILD="$RUN/workers/w2/invocations/000/child.pid"
for _ in $(seq 1 50); do [ -s "$CHILD" ] && break; sleep 0.1; done
sleep 3
"${AC[@]}" status w2 | jq '{state, tmux_alive, idle_s}'
"${AC[@]}" kill w2 | tee "$RUN/w2-kill.json" | jq '{state, exit_code, tmux_alive}'
check "w2 reported killed" jq -e '.state == "killed" and .tmux_alive == false' "$RUN/w2-kill.json"
check "w2 claude process is gone" bash -c "! kill -0 $(cat "$CHILD") 2>/dev/null"
"${AC[@]}" kill w1 | jq '{note, state}'

step "list"
"${AC[@]}" list | jq -c '.[]'

step "result"
echo "evidence: $RUN"
if [ "$FAILS" -eq 0 ]; then echo "ALL CHECKS PASSED"; else echo "$FAILS CHECK(S) FAILED"; exit 1; fi
