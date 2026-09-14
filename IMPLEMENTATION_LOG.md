# Implementation log

Running record of decisions, experiments and kit changes, phase by phase. `DESIGN.md` is
distilled from this at the end. Architecture: Option A — build the missing flowstate + agentctl
machinery, then run the reconciliation as a flowstate graph driven by an orchestrator agent.

Decisions confirmed by the user before implementation (2026-09-14):
1. Credit-note lines appear under their own invoice ID; expected = (original expected − original
   billed); delta = credit issued − expected correction; original line keeps its own delta with an
   offset note; coverage/double-count checks treat the pair together.
2. Scope: July invoices + credit notes correcting a July invoice. `SAGAR-CN-01` (corrects an
   August invoice) is out of scope. Other months are read only for cross-period checks.
3. Underbilled lines: `accept` with a note; expected stays contract-derived.
4. Contract extraction: two independent Opus extractions + agreement/tracing check, cached by
   contract hash + format version + prompt version.
5. One focused commit per phase, after that phase's tests pass.

---

## Phase 1 — agentctl worker lifecycle (2026-09-14)

### Starting state

- `orchestrator/lib/agentctl/` and `orchestrator/lib/agentctl/harnesses/` existed but were empty.
  `orchestrator/bin/agentctl` is a wrapper running `python -m agentctl` with
  `PYTHONPATH=orchestrator/lib`, so the package needed `__main__.py`. No tests existed.
- Every kit shell file is committed as mode 100644 (not executable): both `bin/` wrappers,
  `setup.sh`, and the demo-flow gate/scripts. `bin/agentctl` failed with "permission denied".
- Toolchain: Python 3.12 venv (pyyaml, jsonschema, pydot preinstalled), tmux 3.7c, jq 1.7.1,
  Claude Code 2.1.270. Graphviz `dot` is absent (pydot parses DOT without it).

### What was built

| Module | Responsibility |
|---|---|
| `cli.py`, `__main__.py` | `spawn wait send kill status list logs`; JSON on stdout (logs: text); errors `{"error"}` + exit 1 |
| `registry.py` | Registry inside the run dir; worker/invocation layout; atomic JSON writes; per-registry tmux names |
| `tmux.py` | Detached session create / exact-match has / kill / list |
| `runner.py` | Runs one invocation inside tmux: env scrub, stdin prompt, tee stdout/stderr, signal forwarding, `result.json` then `exit_code` |
| `lifecycle.py` | State derivation, stall detection, wait, send (resume), kill (process-group aware) |
| `transcript.py` | stream-json parsing: session id, cost, turns, error, init evidence |
| `harnesses/claude.py` | Claude Code headless command with isolation flags |
| `harnesses/fake.py`, `fake_worker.py` | Deterministic scripted worker emitting the same event shapes |

Registry layout (`runs/<run_id>/workers/<worker_id>/`): `meta.json`, `prompt.md`, aggregate
`transcript.jsonl` + `stderr.log`, `killed.json` (if killed), and `invocations/NNN/` (000 = spawn,
001+ = send) with `input.md`, `command.json`, `transcript.jsonl`, `stderr.log`, `runner.pid`,
`child.pid`, `result.json`, `exit_code`.

### Decisions and why

- **Headless `claude -p --output-format stream-json`, not an interactive TUI driven by
  `tmux send-keys`.** Completion is a process exit plus a `result` event rather than screen
  scraping; `send` is `--resume <session_id>`, which continues the same conversation. tmux is still
  used so workers outlive the spawner and a human can attach.
- **A Python runner inside tmux instead of a shell pipeline.** Avoids quoting prompts through a
  shell, forwards signals to the child's process group (`start_new_session=True`), and makes the
  completion protocol explicit: `result.json` first, `exit_code` last as the marker.
- **Registry in the run directory** so every prompt, transcript, stderr, pid and exit code is run
  evidence. tmux names carry a hash of the registry path so two runs can both have a worker named
  e.g. `research`.
- **Derived states instead of stored ones**: running / stalled / exited / failed / killed / lost,
  computed from `exit_code`, `killed.json`, tmux liveness and transcript mtime. tmux liveness is
  read *before* `exit_code` so a worker that just finished can never be misreported as lost.
- **Stall = no transcript growth for `stall_after_s`** (default 600 s, per worker or per `wait`).
  `wait` returns `stalled` but does not kill: responding is the orchestrator's judgement.
- **`wait` exits 0 for every outcome**; the JSON `outcome` field carries it. Callers parse one
  shape rather than mapping exit codes.
- **`send` refuses a running/stalled worker** (one invocation at a time keeps transcripts
  ordered) and is allowed after exit, failure or kill.
- **Tool policy enforced in code**: tools must be a subset of Read/Write/Edit/Glob/Grep; anything
  else (Bash, WebFetch, Agent…) is rejected before anything is written. `bypassPermissions` is
  rejected for workers.
- **Environment scrub.** This orchestrating session exports `CLAUDECODE`,
  `CLAUDE_CODE_SESSION_ID`, `CLAUDE_CODE_MESSAGING_SOCKET`/`TOKEN`, `CLAUDE_CODE_BRIDGE_SESSION_ID`,
  etc.; the tmux server inherits them from whoever starts it. The runner removes every `CLAUDE*`
  variable except provider/config ones (`CLAUDE_CONFIG_DIR`, `CLAUDE_CODE_USE_BEDROCK/VERTEX/FOUNDRY`,
  `CLAUDE_CODE_OAUTH_TOKEN`); `ANTHROPIC_*` is kept.
- **Fake harness scripts are JSON with `${name}` templates** (not `{name}`), so JSON content in
  `write` steps needs no escaping. Captures read the rendered prompt, so later flowstate tests can
  have the fake write to whatever output path the prompt names.

### Experiments (evidence under `runs/_*`, gitignored; scripts committed)

**stream-json shape.** `-p --output-format stream-json` requires `--verbose` (CLI error without it).
The `system/init` event carries `session_id`, `tools`, `skills`, `slash_commands`, `plugins`,
`mcp_servers`, `memory_paths`, `messaging_socket_path`; the `result` event carries
`total_cost_usd`, `num_turns`, `is_error`, `subtype`, `permission_denials`. `--session-id` is
honoured and `--resume` keeps the same id.

**Isolation** (`orchestrator/tests/live/probe_isolation.py`, haiku). A fixture project inside the
repo plants a canary token in `CLAUDE.md` and a canary project skill; the repo's own `CLAUDE.md`
("BlueFin") is an ancestor. Evidence is the worker's own init event plus its report of canaries.

| Config | CLAUDE.md canary / BlueFin | Skills listed | Auto-memory | Verdict |
|---|---|---|---|---|
| baseline (`--tools ""`) | reported / YES | 17 incl. canary skill | repo memory dir | leaks (proves probe works) |
| `--setting-sources ""` | NONE / NO | 16 bundled | repo memory dir | skills + memory leak |
| + `--disable-slash-commands` | NONE / NO | 0 | repo memory dir | memory leaks |
| `--safe-mode` only | NONE / NO | 16 bundled | none | skills leak |
| file tools + sources "" + no-slash + strict MCP (+ add-dir data or repo) | NONE / NO | 0 | repo memory dir | memory leaks |
| **final: the above + `--safe-mode`** | **NONE / NO** | **0** | **none** | **isolated** |

Findings that changed the plan: `--setting-sources ""` alone is *not* enough (bundled skills stay);
`--safe-mode` alone is not enough either; and without `--safe-mode` every worker is pointed at the
repo's shared auto-memory directory, which would let one run influence the next. `--add-dir` on
the repo root does not pull the repo `CLAUDE.md` back in. Each worker gets its own messaging socket
(different pid from the orchestrator's), so the scrub prevents attaching to the parent's.

**Live lifecycle** (`orchestrator/tests/live/smoke_claude_worker.sh`, haiku, permission mode `auto`),
all checks passed: spawn in tmux → wait (`exited`, wrote `hello.json` via Write) → worker states
it has no Bash/web tool, no `bash-was-here.txt`, only Write was called → init shows only the five
file tools, no skills/slash commands/MCP, `memory_paths: null` → send → wait (`exited`, same session
id on both invocations, worker recalls the file) → status/logs → kill of a mid-run worker (`killed`,
exit 143, tmux session and claude process gone) → kill on a finished worker is a no-op.
Total cost ≈ $0.01.

**Budget.** `--max-budget-usd 0.0001` → result `subtype: error_max_budget_usd`, `is_error: true`,
agentctl outcome `failed`. The cap is checked between API calls, so spend overshoots by up to one
call ($0.000967 here). Flowstate should treat the cap as a guard against runaway workers, not an
exact limit.

### Tests

`(cd orchestrator && .venv/bin/python -m pytest)` — 21 tests, ~7 s, no tokens: claude command
construction (isolation flags, spawn vs resume, budget/model/add-dir), tool policy, env scrub,
transcript parsing, fake worker determinism and failure modes, and real-tmux lifecycle with the
fake harness (roundtrip + resume, failure, stall → kill with child process gone, steady output not
stalled, timeout, send-while-running rejected, send after kill, lost runner, registry scoping and
id/UUID validation, list/logs, CLI JSON errors). `pytest` added via `requirements-dev.txt`
rather than changing the kit's `requirements.txt`.

### Kit changes

- `orchestrator/bin/agentctl`: executable bit set.
- `.gitignore`: `runs/_*/` (scratch probe/smoke/test runs) and `.pytest_cache/`.
- `CLAUDE.md`: agentctl section, test commands, updated kit state.

### Carried into later phases

- `bin/flowstate`, `setup.sh` and demo-flow scripts are still non-executable; flowstate should
  either set the bit or invoke scripts through `bash` (Phase 2 decision).
- `smoke-branch.dot` references missing `scripts/reduce.sh` (Phase 3).
- Flowstate must check each agent output's `_session_id` against the id it assigned via agentctl.
- Permission mode `auto` (used by the demo DOT files) worked for file writes in the worker cwd.
