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

---

## Phase 2 — flowstate core runtime (2026-09-14)

### Starting state and what the existing flows dictate

- `orchestrator/lib/flowstate/` was empty; `bin/flowstate` runs `python -m flowstate` (not executable).
- pydot facts: `graph [label=...]` is returned as a pseudo-node named `graph`; attribute values keep
  their surrounding quotes; `//` comments parse fine. Neither demo flow uses edge conditions.
- `research.md` says "write to `{research_brief}`" — a variable the same node only *sets*. So a
  node's own `sets_variables` paths must be bound before it runs and committed only after
  validation. `worker_a.sh` writing to `$FLOWSTATE_VAR_note_a` confirms the convention.
- Both prompts ask for `"<your session id>"`, but a Claude worker is never told its session id, and
  the schemas require `_session_id`. The runtime must supply it without editing the prompts.
- `smoke-branch` uses `fork`/`join` (Phase 3) and references a missing `scripts/reduce.sh`.

### What was built

| Module | Responsibility |
|---|---|
| `loader.py`, `validate.py`, `model.py` | Parse DOT + flow.yml into typed nodes/edges/schemas; collect every static issue before a run |
| `templating.py`, `conditions.py` | `{var}` / `{include:path}` rendering; hand-written condition parser/evaluator (no eval) |
| `state.py` | Run layout, atomic `state.yaml` writes under a lock, append-only events, advance lease |
| `engine.py` | init, the `advance` loop, finish → route → gates → commit, retry, respawn, pause/resume/abort, status/events |
| `agent_node.py`, `script_node.py`, `gates.py`, `outputs.py`, `procs.py` | Node execution via agentctl or subprocess, gate runs, output validation/binding/snapshots |
| `runtime.py` | Situation shape and options, retry feedback message, prompt footer, flow-digest check |
| `cli.py`, `paths.py` | JSON CLI; flow/run/prefs resolution |

Run directory `runs/<run_id>/`: `state.yaml` (authoritative: run status, flow paths + digest, config,
committed variables, cursor, per-node status/attempts/retries/output paths, pending situation, pause,
abort), `events.jsonl`, `artefacts/`, `workers/` (the agentctl registry, so `workers/<node>/prompt.md`
is the rendered prompt), and `logs/` (added: script stdout/stderr, gate evidence, rejected-output
snapshots).

### Decisions and why

- **Adapt to the flows, never the reverse.** Node kind is inferred from existing syntax (`Mdiamond`,
  `Msquare`, `prompt_template`, `runner=script`). Demo flows are unmodified.
- **Session id via a runtime footer** appended to every rendered agent prompt ("Your session id is
  … use exactly this string"), also available as `{_session_id}`. The `_session_id` check is
  mandatory for every JSON output of an agent node: proof the file came from the worker flowstate
  started. The real Claude run confirmed workers pick it up.
- **Scripts and gates run through their `#!` interpreter** (falling back to the exec bit). The kit
  committed them non-executable; this makes them work without changing file modes.
- **Templating**: only `{identifier}` and `{include:relative/path}` are placeholders, so JSON examples
  in prompts are untouched; no escape syntax exists, so literal text is never rewritten. Missing
  values are errors. Includes are verbatim and confined to the flow directory.
- **Conditions**: `== != < <= > >=` over variables and string/number/boolean/null literals, joined
  by `and`/`or` (no parentheses). Booleans are not numbers; ordering needs two numbers or two
  strings; unknown variables are errors. Demo flows use none, but routing needs values, so
  `sets_variables` gained a `{file, pointer}` form (JSON pointer). The plain file-name form is unchanged.
- **Routing before gates.** Conditions must select exactly one outgoing edge (zero → `no_route`,
  several → `ambiguous_route`), and only that edge's gates run. Statically, a node with several
  outgoing edges needs a condition on each; parallel fan-out is `runner=fork` (Phase 3).
- **One atomic commit** after outputs exist → schema-valid → `_session_id` → bindings → route →
  gates. Anything else is a persisted situation; rejected outputs are copied to
  `logs/<node>/attempt-N/rejected/` and never committed.
- **Persisted vs transient situations.** Situations needing a decision persist in `state.yaml` and
  repeated `advance` calls return them without re-executing anything. `worker_running` (bounded by
  `--max-wait`), `worker_stalled`, `worker_timeout`, `busy` and `flow_changed` are recomputed each
  call, so "keep waiting" is just `advance --stall-after <longer>`.
- **`advance --max-wait`** exists because the orchestrator agent's shell calls have timeouts (Claude
  Code's Bash tool caps at 10 minutes); long workers are polled in bounded calls.
- **One retry budget per node** shared by `retry` and `respawn` (`max_retries`, default 2 in prefs).
  Exhaustion yields `retries_exhausted`, whose only option is abort.
- **Retry** sends the errors, gate stderr, the required output paths, the session id and optional
  orchestrator feedback into the *same* conversation via `agentctl send`. Listing output paths was
  added after a test showed a worker cannot recover its paths from a message that omits them.
  Script retry reruns the script.
- **Respawn** kills a still-running worker, starts `<node>.respawn-N` with a new session and records
  `replaces_worker_id` / `replaces_session_id`; old worker directory and output snapshot are kept.
- **Crash safety.** An agent attempt is written with `launch: pending` before agentctl is called.
  Recovery checks the registry (spawn) or the invocation count (retry) so nothing launches twice. A
  script found mid-run with no recorded exit is `node_interrupted`, not silently rerun.
- **Locks.** `.state.lock` guards every read-modify-write; `.advance.lock` is a non-blocking lease
  for `advance`/`retry`/`respawn` (a second driver gets `busy`). `pause`/`abort`/`status` need no
  lease, so an operator can stop a run mid-wait. Pause leaves workers running; abort kills them.
- **Flow digest** (sha256 over DOT, flow.yml, prompts, scripts, gates, schemas, includes) is
  recorded at init; a changed definition makes `advance` return `flow_changed`, because outputs
  would no longer be reproducible from the recorded definition.
- **Inferred semantics, documented as such:** `pause_at` is `never|optional|always`, `optional`
  pausing only when prefs/`--supervision` is `high`. `factory-prefs.yml` is created from the example
  on first use; extra keys `max_retries`, `stall_after_s`, `script_timeout_s` have defaults.
- **Static validation** covers everything listed in the Phase 2 brief plus a dataflow pass (a
  variable must be set on *every* path to its use). Joins use the union of their branches and a
  join's `summary_var` counts as produced, so smoke-branch now reports only the true Phase 2 gaps
  (`unsupported_runner`, missing `reduce.sh`). Cycles are rejected for now.
- **Fake harness per run**: `init --harness fake --fake-script NODE=PATH`; flow files stay unchanged.

### Tests

98 tests, all passing, no tokens: 21 from Phase 1 plus 77 for flowstate — language (25: templating,
includes, conditions including rejection of `__import__`, calls, `=`, parentheses), loader (24: the
real smoke-test loads as-is; smoke-branch reports only Phase 3 gaps; 19 targeted static-validation
failures), engine (28: init layout and input errors, script success/failure/retry, gate failure
blocks commit, invalid output never moves downstream, condition routing and `no_route`,
interrupted script, flow change, advance lease, CLI JSON; and via agentctl + fake harness + tmux:
smoke-test end to end with exact event sequence, validation_failed → retry in the same conversation,
`_session_id` mismatch, respawn with old/new sessions, retry budget, worker failure → retry, stall →
respawn kills the old worker, pause/resume and `pause_at` under high supervision, abort kills
workers, recovery in a new CLI process without duplicate work, crash between recording and spawning).

### Smoke tests

- **Fake harness, unchanged smoke-test flow**: completed; events `run_created → node_started/
  node_completed (start) → node_started, worker_started, worker_completed, outputs_validated,
  gate_passed, node_completed (research) → same for summarise (no gate) → run_completed`.
- **Forced validation failure** (fake research worker writes 1 bullet, schema needs 3–5):
  `advance` → `validation_failed` (`/bullets … is too short`, rejected output snapshotted) → repeated
  `advance` returns the same situation without re-running → `retry --feedback` → agentctl shows one
  worker, 2 invocations, 1 session id → `advance` → completed.
- **Real Claude** (`runs/_phase2-smoke/claude-smoke-20260914-180252`, flow unchanged, `sonnet`,
  permission mode `auto`): completed. Both workers (`claude-sonnet-5`) wrote schema-valid outputs
  whose `_session_id` matched the id flowstate assigned; the gate ran through `/bin/bash`; init
  events show only file tools, 0 skills, `memory_paths: null`. Cost $0.0239 + $0.0245.

### Deviations from the plan

- Added `runs/<run_id>/logs/` next to the four planned entries.
- `sets_variables` `{file, pointer}` form; `and`/`or` in conditions.
- Situations split into persisted and transient; `advance --max-wait`.
- `retry` and `respawn` share one budget; flow-digest check added.
- Cycles rejected (the plan did not specify).

### Known limitations (carried forward)

- No fork/join/dynamic_fanout (Phase 3); one cursor, acyclic graphs only.
- Script nodes run inside the `advance` process, so a crash mid-script needs an explicit retry.
- Stall detection covers agent workers only; gate timeout is a fixed 120 s.
- Output validation assumes JSON files with schemas; non-JSON outputs (e.g. memos) need an
  extension in Phase 6.
- The graph-orchestrator skill that responds to situations does not exist yet (Phase 4).

---

## Phase 3 — fork, join and dynamic_fanout (2026-09-14)

### Starting state and what the kit dictates

- `smoke-branch.dot`: `fork → worker_a, worker_b → j (runner=join, reducer_script="scripts/reduce.sh",
  summary_var="branch_summary") → merge_notes`. The join's description says the reducer fires "on each
  arrival to maintain branch_summary"; `merge_notes.sh` reads `note_a`/`note_b` "set by flowstate scope
  merge"; `branch_summary` is `type: dict`. `scripts/reduce.sh` is missing from the kit.
- The kit has no `dynamic_fanout` example, so its attributes (`items`, `max_items`, `max_parallel`)
  were designed here, following the plan ("reads a JSON list from a variable… `{_branch_id}` and
  `{item}` available… an empty list skips straight to the join").
- Phase 2 ran script nodes synchronously inside `advance`, which cannot run branches in parallel or
  survive `advance` being killed mid-script.

### What was built

| Module | Change |
|---|---|
| `scope.py` (new) | `Scope`: where a node's state lives — state.yaml root or `parallel.<p>.branches.<b>` — so node execution is written once |
| `execution.py` (new) | Agent/script node execution extracted from Phase 2 `engine.py`, scope-aware, as non-blocking steps (`progress`/`waiting`/`blocked`/`arrived`/`transient`/`decision`) |
| `parallel.py` (new) | Branch creation, the branch driver, ordered reducer folding, merge, join completion |
| `script_runner.py` (new), `procs.py` | Detached script execution with an exit marker; pid liveness that reaps our own children; group kill |
| `engine.py` | Top-level cursor loop delegating to execution/parallel; retry/respawn/resume with branches; abort kills branch workers, scripts and reducers; status `parallel` summary |
| `loader.py`, `validate.py`, `model.py` | Fork/join/fan-out attributes, `Region` analysis, region rules, branch-aware availability, node-id format, reserved `item` |
| `runtime.py` | Branch fields on situations, new situation kinds, `run_level_situation` moved here |
| `state.py` | libyaml loader/dumper when available |
| `cli.py` | `--branch` for `retry` and `respawn` |
| `smoke-branch/scripts/reduce.sh` | Minimal kit fixture exercising the reducer contract |
| `factory/flows/smoke-fanout/` | New demo flow for `dynamic_fanout` (agent + gate + script inside the template, reducer, list merge) |

### State model (extends Phase 2; Phase 2 keys unchanged)

```yaml
cursor: fan                      # stays on the parallel node while its region runs
nodes:
  fan: {kind: dynamic_fanout, status: running, ...}
  describe: {kind: agent, status: in_branches, parallel: fan}   # marker only
parallel:
  fan:
    kind: dynamic_fanout         # or fork
    join: collect
    status: running              # running | merged | completed
    items_var: items
    items: [alpha, beta, gamma]  # the list as consumed; never re-read
    max_parallel: 3
    branch_order: [fan-0000, fan-0001, fan-0002]
    branches:
      fan-0001:
        index: 1
        entry: describe
        status: completed        # pending | running | awaiting_decision | completed
        cursor: null             # node executing now; null once arrived at the join
        context: {_branch_id: fan-0001, _branch_index: 1, _parallel_node: fan,
                  _run_artefact_dir: <run>/artefacts/branches/fan-0001, item: beta}
        variables: {description: ..., stamp: ...}   # produced in this branch only
        nodes: {describe: <Phase 2 node record with attempts/workers>, stamp: ...}
        situation: null          # a persisted branch situation, if any
    join_state:
      status: completed          # waiting | merged | completed
      folded: [fan-0000, fan-0001, fan-0002]
      summary: {...}             # summary_var so far
      reducer_runs: [{run, branch_id, fold, log_dir, runner_pid, outcome, exit_code}]
      merged_variables: {description: [...], stamp: [...], fanout_summary: {...}}
```

This answers, without replaying events: which parallel node is active (cursor + `status`), which
branches exist and their items, each branch's current node, completed/failed branches, workers per
branch (attempt `worker_id`s), pending branch situations, whether the join completed, and the merged
variables.

### Semantics and decisions

- **Regions.** A fork/fan-out owns the nodes between it and exactly one join. Rejected statically:
  branches reaching `done`/`start` without the join (`branch_escapes`), several joins
  (`unmatched_join`), a join with inputs from outside its region or closing two regions
  (`invalid_join`), joins closing nothing (`orphan_join`), shared fork nodes
  (`overlapping_branches`), entry from outside (`branch_entered_from_outside`), fork/fan-out inside a
  region (`nested_parallel`), direct `fork -> join` (`empty_branch`), conditions/gates on edges leaving
  the parallel node (`invalid_branch_edge`), overlapping fork variables or region variables produced
  outside (`branch_variable_conflict`), branch builtins used outside a region
  (`branch_variable_outside_region`). All existing Phase 2 checks still run.
- **Branch ids** are deterministic and persisted at creation: fork `<fork>-<entry>` in sorted entry
  order; fan-out `<fanout>-NNNN` by list position (four digits minimum). Items are stored in state, so
  a restart never re-reads or re-interprets the list, and completed branches are never recreated.
- **Isolation.** Inside a branch `_run_artefact_dir` is `artefacts/branches/<id>/` (the existing
  `{_run_artefact_dir}/…` output templates then land per branch with no flow changes), logs go to
  `logs/branches/<id>/`, agent workers are `<node>.<id>` with their own registry directory, and
  agentctl isolation is unchanged. A test gives three fork branches the same output file name.
- **One driver, real parallelism.** `advance` still holds the single lease and makes every state
  write; concurrency comes from what it observes — agent workers in tmux and scripts/reducers in
  detached runners. Each pass steps every branch without blocking and starts new branches only while
  fewer than `max_parallel` are running. Tests check overlap by timing (fork) and by counting running
  branches at `--max-wait` (fan-out); the real Claude run shows two workers starting 39 ms apart.
- **Script nodes became detached everywhere** (top level too), via `script_runner.py`, which writes
  `script.exit_code` after `script.result.json`. This makes script branches parallel and lets a killed
  `advance` recover a still-running script instead of reporting it interrupted. All Phase 2 tests pass
  unchanged in behaviour.
- **Failure reporting.** A branch situation is persisted on that branch; siblings keep running. Once
  no branch can make progress (or `--max-wait` elapses), `advance` returns the first branch situation
  in branch order with `branches` counts and `other_branch_situations`. The join never completes while
  any branch is awaiting a decision. `retry`/`respawn` take `--branch`; without it the branch is
  inferred when exactly one matches, otherwise `branch_required` lists the candidates.
- **Merge.** Fork: branch variables merged by name (disjointness is enforced statically). Fan-out:
  every region variable becomes a list in branch order. The join's own `summary_var` is the folded
  summary. Values then flow through normal routing and gates from the join.
- **Reducer.** Optional; requires `summary_var` (a dict). Runs once per branch, strictly in branch
  order: branch *k* is folded once it and every earlier branch have arrived — "fires on arrival"
  while keeping the result independent of arrival timing. Contract: env
  `FLOWSTATE_REDUCER_SUMMARY_IN` (JSON, `{}` initially), `FLOWSTATE_REDUCER_BRANCH_VARS`,
  `FLOWSTATE_BRANCH_ID`/`_INDEX`, the branch's `FLOWSTATE_VAR_*`; it must write a JSON object to
  `FLOWSTATE_REDUCER_SUMMARY_OUT`. Evidence per run in `logs/joins/<join>/fold-NNNN-<branch>-runN/`
  (command, stdout, stderr, result, exit code, summary in/out, branch variables). Non-zero exit or
  timeout → `reducer_failed`; missing/non-object output → `reducer_output_invalid`; vanished runner →
  `reducer_interrupted`. These are top-level situations on the join; `retry <join>` reruns that fold.
- **Empty fan-out**: `fanout_empty` event, no branches, join completes immediately; region variables
  are `[]` and `summary_var` is `{}` (the reducer's initial summary; the reducer does not run).
- **Items**: a `list` variable, or a `path` to a JSON list file. Not a list, unreadable, or more
  than `max_items` → `fanout_invalid` (abort only), before any branch exists.
- **Atomic join completion.** `route_and_commit` gained `extra_commit`, so marking the join, the
  parallel record and the fork node completed happens in the same write that moves the cursor.
- **Evidence without secrets.** `command.json` for detached runs records only `FLOWSTATE_*`
  variables; the runner inherits the rest of the environment and nothing else is written to disk.

### Surprise found while building

- The first smoke-branch run returned `branches_running` after one reducer fold: the driver treated a
  fold that had just *completed* as idle. Fixed by stopping only when no branch is live and the
  reducer has nothing to do, and re-stepping immediately after fold progress.

### Tests

140 tests, all passing, no tokens (98 from Phases 1–2 plus 42 new in `test_flowstate_parallel.py`).
Three Phase 2 tests encoded "smoke-branch is invalid" and were updated to the new truth: the loader
test now checks smoke-branch's fork region; the init "invalid flow" case uses `flow_not_found`, with
an explicitly broken flow asserting `invalid_flow` and `validate` exit 1.

New coverage:
- **Static**: valid fork, fan-out and agent-fork flows; smoke-fanout's region; 25 targeted invalid
  definitions (missing reducer, reducer without summary_var, non-dict summary_var, orphan/foreign/
  unmatched joins, escaping, nested, overlapping, externally entered and empty branches, variable
  conflicts, `item`/`_branch_id` outside regions, reserved `item`, conditions on fork edges, one-edge
  fork, `max_parallel=0`, missing/undeclared/non-list `items`, two template edges, `max_items=0`, a
  template not reaching its join, unknown fan-out attribute).
- **Fork/join/reducer** (script branches): parallel execution with merge by name, isolated
  same-named artefacts and fold order independent of arrival order; one failing branch blocks the
  join, retry via CLI `--branch` reruns only that branch; join waiting persists and resumes in a new
  process; reducer failure with full evidence then retry; reducer output that is not an object.
- **Fan-out** (agent branches via fake harness + tmux): 3 items with `{item}`/`{_branch_id}`/
  `{_branch_index}` rendering, list merge order, session ids and status rows; 1 item; 0 items;
  one of five branches failing validation retried alone (others keep one invocation);
  two failing branches requiring `--branch`; partial completion under `max_parallel`;
  12 deterministic ids never recreated; items from a JSON file, non-list file and `max_items`;
  abort killing running branch workers.
- **Recovery**: SIGKILL of `advance` while an agent branch is still working (Case A: the other two
  completed; the worker is reconnected; exactly 3 workers and 1 invocation each); SIGKILL while a
  script branch is running (the detached script finishes; not rerun).

### Smoke tests

- **smoke-branch** (kit flow + new reducer fixture): completed. Folds `fork-worker_a` then
  `fork-worker_b`; `merge_notes` read both branch notes from `artefacts/branches/fork-worker_*/`;
  `branch_summary` committed.
- **smoke-fanout, fake harness**: 3 items (three parallel workers, gate per branch, summary folded in
  order, report lists alpha/beta/gamma), 1 item, and 0 items (`fanout_empty`, `stamp: []`,
  `fanout_summary: {}`, report count 0) all completed.
- **Real Claude**: smoke-test (regression) completed, $0.0212 + $0.0259; smoke-fanout with
  `item_names=lighthouse,glacier` on haiku completed — both branch workers wrote correct item/branch
  ids with matching session ids, started 39 ms apart and finished 7.6 s and 8.7 s later (overlapping),
  the in-branch gate passed, report correct, $0.0106 + $0.0120. Worker init events still show only
  file tools, 0 skills, no auto-memory.

### Deviations from the plan

- Nested parallel regions are rejected (not in scope for Phase 3; no planned freight step needs them).
- Script nodes run detached everywhere, not inside the `advance` process.
- New directories `artefacts/branches/`, `logs/branches/`, `logs/joins/`.
- `max_parallel` limits running *branches*, not individual processes.
- `item` is a reserved variable name; `items` may also be a path to a JSON list file.

### Known limitations (not claimed as tested)

- Implemented but not covered by tests: `pause_at` on nodes inside branches; respawn of a branch
  worker; `worker_stalled`/`worker_timeout` inside branches; a gate failure on the join's outgoing edge
  followed by `retry <join>`; `null` entries in merged fan-out lists when a branch's internal
  conditional path skips a producer. Gates inside branches are exercised by the smoke-fanout demo,
  not by a unit test.
- Scale is unmeasured beyond 12 branches in tests: every transition rewrites all of `state.yaml`, and
  each pass reads it once per branch step, so cost grows with branch count (libyaml helps).
- Every script receives all variables as `FLOWSTATE_VAR_*`; very large list/dict values could exceed
  OS environment limits.
- Acyclic graphs only; one `advance` driver per run.

---

## Phase 4 — graph-orchestrator skill (2026-09-14)

### Starting state

- No `.claude/` directory existed, although `PROBLEM.md` and `brief.md` both reference
  `.claude/skills/graph-orchestrator/SKILL.md` ("a minimal orchestrator skill with just enough machinery to
  progress a flowstate graph"). The Phase 4 brief's `.claude/skills/graL.md` was read as that path.
- The skill was written against the real CLI (`flowstate <cmd> --help`) and `runtime.OPTIONS` (24
  situation kinds), not the original plan. Four runtime facts shaped it:
  1. `retry` is refused while a worker is running or stalled (`worker_busy`).
  2. `retry` and `respawn` share one per-node `max_retries` budget, so an exhausted node cannot be respawned.
  3. Many situations list only `abort` in `options`, but `pause` is always available while a run is active.
  4. Situations did not report the budget, so an orchestrator would have had to count retries itself.

### What was built

| Piece | Purpose |
|---|---|
| `.claude/skills/graph-orchestrator/SKILL.md` | The procedure: role, allowed commands, forbidden actions, supervision loop, decision order, retry vs respawn, feedback rules, branch supervision, pause and final-report templates |
| `.claude/skills/graph-orchestrator/situations.md` | Reference: every situation (meaning, fields, evidence, moves, default decision) and every command error code |
| Flowstate: `scope.py`, `engine.py`, `parallel.py`, `runtime.py` | Node records store their effective `max_retries`; every node situation reports `max_retries` and `retries_remaining` |
| Flowstate: `cli.py` | Help text now shows `--branch` / `--feedback-file` and that `--max-wait` returns any `*_running` situation |
| agentctl: `harnesses/fake_worker.py` | Steps may carry `when` / `unless` prompt regexes, so one fake script can fail for exactly one fan-out item |
| `orchestrator/tests/fixtures/flows/orchestrator-drill/` + fakes | Deterministic drill: top-level `validation_failed`, one branch `validation_failed`, a gate, a script (with a `DRILL_FINISH_FAIL` hook) |
| `orchestrator/tests/test_orchestrator_skill.py` | Contract tests and scripted-supervisor protocol tests (token-free) |
| `orchestrator/tests/live/orchestrate_drill.sh` | A real Claude orchestrator loads the skill and supervises the drill (`SCENARIO=recover` or `pause`) |

### How the skill works

- **Division of labour.** The graph states guarantees; Flowstate and agentctl do the mechanics; the
  orchestrator only interprets situations and picks one Flowstate command. The skill contains no domain logic.
- **Loop.** `status` (or `validate` + `init` with human-provided inputs) → `advance RUN --max-wait 300` →
  classify → inspect the evidence the situation names → one command → advance again. It stops on
  `completed`, `aborted`, `paused`, or after pausing for a human.
- **Decision order**, first match wins:
  1. Finished or waiting: stop, or advance again.
  2. Budget spent (`retries_exhausted` or `retries_remaining` = 0): pause.
  3. Definition, input or routing problems (`render_failed`, `condition_error`, `no_route`,
     `ambiguous_route`, `fanout_invalid`, `flow_changed`): pause.
  4. Worker or output problems: retry, respawn or pause.
  5. Deterministic code failures (`script_failed`, gate on script output, `reducer_failed`,
     `reducer_output_invalid`): pause unless the evidence shows a transient cause.
  6. Unknown outcome (`node_interrupted`, `reducer_interrupted`): retry once, then pause.
  7. Anything unclear: pause.
- **Retry vs respawn.** Retry keeps the same worker and session. The feedback must be built from evidence:
  JSON pointer, violated constraint read from the schema, value received, file to rewrite. It never supplies
  content, and never suggests weakening a contract. Respawn starts `<worker>.respawn-N` with a new session and
  keeps the old evidence. For stalled workers: wait once with `--stall-after ≈ 2 × stall_after_s`, then respawn.
  For a single node, a repeat of the same failure escalates to respawn if budget remains, otherwise pause.
  Missing information or authority means pause immediately.
- **Branches.** Always pass `--branch <branch_id>` from the situation, except for reducer situations, which
  belong to the join. Resolve one branch situation, advance, then handle the next. Never touch completed or
  running branches. If several branches fail the same way, suspect a systemic cause and pause.
- **Pausing.** `pause --reason` followed by a fixed report: what happened, where, evidence, why not
  continued, decision needed, how to continue. Pausing keeps the pending situation and budget.
- **Forbidden:**
  - editing run directories or flow definitions, or writing outputs
  - starting or killing workers directly
  - running a second driver
  - keeping a private retry counter
  - aborting on its own initiative
  - any shell use other than one plain `orchestrator/bin/flowstate` command, with no chaining or expansion
    (evidence is read with Read, Glob and Grep)

### Decisions and why

- **Pause, not abort, is the default stop.** Abort is irreversible; pause keeps the pending situation, the
  evidence and the budget, and a test shows `resume` returns to the same situation. The skill aborts only when
  a human says so.
- **Budget lives in Flowstate.** Rather than have the skill count retries (which could disagree with the
  runtime), node situations now carry `max_retries` and `retries_remaining` from the node record that
  `retry` and `respawn` enforce.
- **Contract-bound documentation.** Tests require the situation reference to equal `runtime.OPTIONS` exactly,
  every command and flag quoted in the skill to exist in the argparse parser, and every documented error code
  to be raised somewhere in `flowstate/`. The skill cannot silently drift from the runtime.
- **Scripted `Supervisor` as a test double.** Deterministic tests cannot run a model, so a test-only class
  applies the documented decision procedure literally through the real CLI in subprocesses. This proves the
  command sequences in the skill produce the documented outcomes. Judgement by a real model is exercised
  separately by the live script. The Supervisor is not product code.
- **Loading via the Skill tool in live tests.** A `/graph-orchestrator` prompt expansion does not appear in the
  stream-json transcript, so loading could not be verified. The live prompt therefore asks for the Skill tool,
  which leaves a `tool_use` record.
- **Enforcement in the live harness.** `--tools Bash,Read,Grep,Glob,Skill`,
  `--allowedTools "Bash(orchestrator/bin/flowstate:*)"`, Write/Edit/web disallowed, `--permission-mode dontAsk`,
  `--permission-prompts none`. Probes showed that without `dontAsk`, an arbitrary `ls` ran. With `dontAsk`,
  `touch` was denied (the file was not created) while `ls` and Read still worked. So mutation is prevented, but
  read-only shell viewing is not.

### Tests

153 tests pass, all without tokens: 140 existing plus 13 new.

- **Contract (6):**
  - frontmatter and link to the reference
  - every `flowstate` command and flag quoted in SKILL.md or situations.md exists
  - the situation reference equals `runtime.OPTIONS`
  - SKILL.md mentions every situation
  - documented error codes are raised by the runtime
  - the Never list covers state edits, tmux, agentctl, fabrication, flow edits, private counters and abort
- **Protocol, scripted Supervisor via the real CLI with fake workers (6):**
  1. The drill completes via exactly two retries. Checked: same session IDs on retry; rejected outputs
     preserved; feedback recorded in state, in the `retry_requested` event and in the worker's `input.md`;
     corrected outputs pass `outputs_validated` and their schemas; siblings run once; the only workers are the
     ones Flowstate launched through agentctl.
  2. Retry → respawn → pause escalation. The pending `validation_failed` shows `retries_remaining: 0`, and
     after `resume` the same situation returns.
  3. A stalled worker: wait once with `--stall-after 6.0`, then respawn. Flowstate killed the old worker and
     the run completes.
  4. A gate failure on agent output: retry with the gate script and its stderr quoted. Gate evidence `eval-1`
     exited 1 and `eval-2` exited 0.
  5. A deterministic script failure: pause, and the script is not rerun.
  6. `flow_changed`: pause, and nothing runs.
- **Harness (1):** fake-worker `when` / `unless`.

### Real-agent runs (Sonnet orchestrator, fake workers)

Development iterations, each of which changed the skill or the harness:
- **First `recover` run.** The run completed, but two checks failed. First, the orchestrator put `${PWD}` in
  retry feedback; dontAsk denied that command and the orchestrator reissued it without the expansion. This led
  to the "one plain flowstate command, no shell expansion, single-quoted feedback" rule. Second, skill loading
  could not be proven, which led to loading through the Skill tool.
- **First two `pause` runs.** Decisions were correct (two retries, then a pause with evidence), but the
  orchestrator read evidence with `find`, `find | head` and `cat … 2>/dev/null`. This led to the explicit
  Never rule and a `find` allowance in the viewer check. The drill script's comment also stated the expected
  decision ("retrying cannot fix"); it was removed so the pause has to come from the evidence.

Final runs, against the committed skill text:

| Scenario | Run | Checks | Outcome | Cost |
|---|---|---|---|---|
| recover | `runs/_orchestrator-live/drill-recover-20260914-201220` | 13 / 13 | Skill loaded via Skill tool → status → advance → read rejected output and schema → retry `plan` with pointer, constraint and received value → advance → retry `work --branch fan-0001` → advance → `completed`. No denials, no Write/Edit, same sessions, siblings once. | $0.21, 14 turns |
| pause | `runs/_orchestrator-live/drill-pause-20260914-201312` | 15 / 15 | Same two retries → `script_failed` at `finish` → read stderr and script → `pause` with a reason naming the deterministic cause and the needed human decision → PAUSED report. `script_failed` still pending, script not rerun. | $0.26, 19 turns |

In both final runs the orchestrator still made one plain read-only `find` call for listing, despite the rule.

### Deviations from the plan / brief

- **Stalled workers are not retried.** The runtime refuses (`worker_busy`); the brief's "retry if
  appropriate" becomes wait once, then respawn.
- **After the budget is exhausted, respawn is impossible** because retry and respawn share one budget. The
  skill pauses instead of respawning.
- **The skill never aborts on its own initiative**; the brief allowed "abort if continuing would be unsafe".
- **A small Flowstate addition:** budget fields on situations, plus CLI help text fixes. **A test-harness
  addition:** fake-worker `when` / `unless`.

### Implemented and tested vs inferred

- **Exercised by tests or live runs:** completion; top-level and branch `validation_failed` → retry;
  escalation to respawn and then pause; stalled → wait → respawn; agent `gate_failed` → retry;
  `script_failed` → pause; `flow_changed` → pause; pause preserving the situation; branch-scoped retry leaving
  siblings untouched.
- **Written as guidance, not exercised:**
  - `worker_failed` sub-cases; `worker_timeout`; reducer situations; `node_interrupted` and
    `reducer_interrupted`
  - `render_failed`, `condition_error`, `no_route`, `ambiguous_route`, `fanout_invalid`
  - `busy` handling
  - the "several branches fail alike → pause" heuristic
  - a real orchestrator starting a run itself (`validate` / `init`); the live drills were pre-initialised
  - judgement on genuinely ambiguous business evidence (the drill failures are structural)

### Known limitations

- Judgement quality depends on the model. Live evidence is one Sonnet run per scenario against the final text,
  plus the development runs above.
- Adherence to the "Read/Glob/Grep, not shell viewers" rule is imperfect, as the final runs show. Enforcement
  in the live harness prevents mutation, not reading.
- The skill itself cannot enforce its Never list. Enforcement depends on how the orchestrator session is
  launched: the live script sets tools and permissions, but an interactive session relies on the user's
  permission settings.
- Nothing stops an orchestrator from *reading* `state.yaml`; the skill only forbids writing it.
- Live tests cost about $0.20–0.26 per scenario and are not part of pytest.

---

## Phase 5 — deterministic freight code layer (2026-09-14)

### Scope and starting state

Per the plan, Phase 5 covers discovery, parsers, the clause index, the rate-spec format and pricing
engine, the disposition policy, report assembly, and their tests. There are no agent nodes and no
flow wiring (those are Phases 6–7), and no real rate specs: those come from the Phase 6 extraction
agents. The format inspection covered all 17 invoice documents before any parser was written:
- **Alpine JSON:** one schema with `discount`, `consignment_count` and a generic `handling_fee`.
- **Falcon text:** invoice and credit-note headers, per-consignment blocks, three charge labels, and
  free-text corrections on the credit note.
- **Sagar CSV:** an invoice layout with a `TOTAL` row and no invoice id, plus a separate credit-note layout.

### Where it lives

`factory/flows/freight-reconciliation/`: the `freight/` package, `config/carriers.yml`, `config/policy.yml`,
`definitions/rate-spec.json`, `tests/` and `pytest.ini`. The flow's DOT and flow.yml arrive in Phase 7.

### What was built

| Module | Responsibility |
|---|---|
| `money.py`, `vocab.py` | Exact INR Decimal arithmetic; the shared charge-code vocabulary and the shipment fields a spec may use |
| `parsers/` (`alpine_json`, `falcon_text`, `sagar_csv`, registry) | Strict format adapters producing normalized documents with integrity facts |
| `documents.py` | Carrier config validation, discovery, and the scope manifest |
| `contracts.py` | Clause index: clauses, sections, agreement ref, term, and the numbers in each clause |
| `ratespec.py` + `definitions/rate-spec.json` | Rate-spec schema and semantic checks, band membership, `pricing_view`, `cited_clauses` |
| `pricing.py` | Shipment matching, spec evaluation, flags, credit notes, invoice-level findings |
| `policy.py` + `config/policy.yml` | Flag effects → a disposition, or a bounded judgement |
| `report.py` | Report assembly, deterministic justifications, invariant verification, memo item list |
| `cli.py` | `discover`, `clauses`, `check-spec`, `price`, `assemble` for the Phase 7 script nodes |

### Decisions and why

- **Prices come only from shipment records and a rate spec.** Weight, distance and service as stated on
  the invoice are kept as evidence and compared (`ATTRIBUTE_MISMATCH`), never used to price.
- **Exact arithmetic.** Decimal throughout, decimal strings in JSON. Components are summed exactly and
  the line total is rounded once, half-up. The tolerance (`0.01`) is policy, not code.
- **Strict parsers.** Unrecognised text, headers, totals, dates or formats raise `ParseError`, and an
  orchestrator should pause on that rather than guess. Document-internal inconsistencies are recorded
  as integrity facts, never corrected. On the real data these facts include a line-arithmetic failure
  in three Sagar invoices (reported, not interpreted).
- **Shared charge vocabulary.** Parsers map each carrier's labels to `CHARGE_CODES`, and spec components
  declare which codes they account for. That gives three distinct outcomes: `UNCONTRACTED_CHARGE` (a
  known charge the contract does not provide for), `UNRECOGNIZED_CHARGE` (the parser could not classify
  it), and `CHARGE_NOT_APPLICABLE` (a contracted charge whose condition the shipment does not meet).
- **Carrier master config.** Formats map to carriers, and carriers to contracts and consignment
  prefixes. Adding a carrier means one config entry, plus a parser only if its format is new.
- **Discovery parses everything.** Scope follows the rule confirmed before implementation. Reference
  documents are kept so duplicate billing is detected across periods. Documents whose period cannot be
  established are `unresolved`, never dropped.
- **The clause index records structure and numbers, not meaning.** It supports clause citations, and in
  Phase 6 the tracing check that every spec number appears in the clause it cites.
- **The rate spec is a closed set of building blocks.**
  - Quantities `max`/`min`; components `flat` / `per_unit` / `banded_rate` / `percent_of` (earlier
    components only); shipment conditions `service_level`, `special_handling_includes` and
    `all`/`any`/`not`.
  - Allowed service levels, a term, invoice discounts gated on consignments in the billing month, and
    informational gaps.
  - Semantic checks cover name uniqueness, reference ordering, non-overlapping non-empty bands and the
    term's order.
  - Bands keep the contract's wording. A value in no band is a `CONTRACT_GAP` if it falls between
    bands, or `OUTSIDE_RATE_CARD` if it is beyond all of them; it is never snapped to a band.
  - `pricing_view()` removes citations, descriptions and gaps, and normalises numbers, for the Phase 6
    agreement check.
- **Credit notes** follow the confirmed rule. When several credits correct the same line, earlier
  credits are subtracted (ordered by issue date). The original line is flagged `CREDIT_NOTE_OFFSET`.
  Policy then accepts the original, and the credit line carries the residual: dispute if under-credited,
  accept with a note if over-credited. `report.verify` rejects any disputed line that is offset, so no
  rupee is counted twice.
- **Policy combination.** Escalate > offset > dispute > base outcome. A `judgement` flag turns the
  result into `[outcome, escalate]` for an adjudicator, who can only choose between those and cannot
  change amounts. Every flag must have a policy entry and unknown flags fail closed.
- **Report.** Dispositions come only from policy or from bounded adjudications. Justifications for
  policy-decided lines are generated from amounts and flag messages with sorted clause citations.
  `verify()` recomputes everything and validates `report.schema.json`.

### Interpretations to review (made explicit in policy.yml and the code)

These are business judgement calls, recorded here for `DESIGN.md`:
- **Volume discount.** Computed on the contract-correct (expected) line total. Consignments are counted
  from `shipments.json` by carrier and ship-date month, including shipments not yet delivered.
- **Duplicate billing.** The earliest billing (period, period start, document, line) is payable; each
  later one expects 0 and is disputed.
- **Uncontracted vs unrecognised charges.** An uncontracted but recognised charge is disputed; an
  unrecognised label is escalated.
- **Adjudication.** Not-delivered shipments, service levels the contract does not offer, and invoices
  whose own charges do not add up go to an adjudicator.
- **Invoice total mismatch.** A stated invoice total that differs from the invoice's own lines is
  disputed when it overcharges and accepted otherwise.
- **Report summary fields.** `counts_by_disposition` counts lines only, and `total_billed` includes
  credit notes as negative totals.
- **Credit-line clauses.** A credit-note line cites the clauses behind its original line's expected
  amount.

### Tests

109 freight tests pass without tokens, plus the unchanged 153 orchestrator tests.

| File | Tests | Coverage |
|---|---|---|
| `test_money_and_contracts.py` | 21 | Amounts, rounding, synthetic clause index, malformed contracts, real-contract structure |
| `test_parsers_and_discovery.py` | 17 | Exact synthetic parses of every format, parser strictness, unknown formats, carrier config, the scope rule on synthetic documents, real-file structure, and the real July scope |
| `test_ratespec.py` | 24 | Schema vocabulary alignment, 14 invalid specs, band semantics, `pricing_view`, citations |
| `test_pricing_policy.py` | 32 | Evaluation and rounding, gaps, missing data, every line flag, cross-period duplicates, four credit-note cases, unresolved credits, adjustment and total findings, refused inputs, the policy combination table, failing closed |
| `test_report_cli.py` | 15 | Valid report with each disputed rupee counted once, bounded adjudications, 9 tampering cases caught by `verify`, CLI end to end on synthetic files |

The real data is used only for structure: parsing, ids, periods, line counts, clause numbers, and the
scope rule. No test prices real data or encodes a reconciliation answer.

### Surprises

- A synthetic sanity run caught a keyword collision in `pricing.flag()`: a `code` detail clashed with
  the flag code parameter.
- One test expectation missed that a 5.00 charge billed against a 110.00 contract amount is also
  `UNDERBILLED`. The engine was right; the test was fixed.

### Known limitations

- **Formats:** only the three observed formats are supported; anything else stops discovery.
- **Evidence comparison:** only weight, distance and service are compared, not route cities.
- **Shipment conditions:** these cover service level and special handling only; a contract term needing
  other shipment facts would need the rate-spec format extended, and would fail validation until then.
- **Flow digest:** flowstate's flow digest covers the scripts a flow references, not the `freight`
  package they import. Phase 7 must record the package's hash so a code change mid-run is detected.
- **Placeholder interfaces:** the adjudication input (`{item_id: {disposition, justification,
  contract_clause}}`) and `memo_items()` are placeholders for Phase 6 agents.
- **Untested at scale:** the code is pure Python and linear in lines, but not measured beyond the sample.

---

## Phase 6 — agent nodes: extraction, agreement and cache, adjudication, memos (2026-09-14)

### Scope and starting state

The plan's Phase 6 row: "Agent nodes: extraction, agreement check and caching, adjudication, memos, and
their gates — each node passes its gate in isolation." Flow wiring, the reconcile skill and the July dry
run are Phase 7, so nothing here produces a reconciliation.

The phase resumed from uncommitted partial work, inspected before continuing: the rate-spec schema had
gained `non_pricing` and `unrepresentable`, and `tracing.py`, `agreement.py`, `grounding.py` and `audit.py`
existed, with no tests, no CLI wiring and no nodes using them. They were kept and completed; everything
else below was built in this phase. Both suites passed on that starting state (153 orchestrator, 109 freight).

### What was built

| Piece | Responsibility |
|---|---|
| `tracing.py` | A spec is anchored in its contract: identity (carrier, file, agreement ref, term), clauses exist, every priced number appears in a clause the element cites, every clause is accounted for, conditions use shipment vocabulary |
| `agreement.py` | Two specs agree if the pricing engine behaves identically on probe shipments (amount, gap flags, per-charge-code status, service offered) and on invoice adjustments around every threshold |
| `rules.py` | Extraction assignments, round 1 and round 2 agreement, adoption, the cache |
| `audit.py` | From a worker's own transcript: only file tools, reads limited to assigned inputs and its branch, writes limited to its branch |
| `grounding.py` | Every figure in agent prose must be copied from the facts packet it was given |
| `adjudication.py` | Packets for open items, decision check, merge |
| `memos.py` | Facts per non-accept row, draft check, markdown rendering with a code-generated facts table |
| `cli.py` | `rules-plan`, `rules-agree`, `rules-final`, `audit-worker`, `adjudication-plan`, `check-adjudications`, `merge-adjudications`, `memo-plan`, `check-memos`, `render-memos`; `check-spec` now traces |
| `prompts/` | `extract-rules.md` (+ `reference/rate-spec-guide.md`, schema included), `adjudicate.md`, `write-memos.md` (+ `reference/memo-style.md`) |
| `gates/` | `rate-spec-traced.sh`, `worker-inputs-only.sh`, `adjudications-grounded.sh`, `memos-grounded.sh` |
| `scripts/` | `rules-plan.sh`, `rules-agree.sh`, `rules-final.sh`, `adjudication-plan.sh`, `merge-adjudications.sh`, `memo-plan.sh`, `render-memos.sh` |
| `definitions/` | agent outputs `adjudications`, `memo-drafts`; script outputs `rules-plan`, `rules-agreement`, `rules-final`, `adjudication-batches`, `adjudications-merged`, `memo-batches`, `memos-index` |
| `tests/stages/`, `tests/stage_flows.py` | Isolation flows (`stage-rules`, `stage-adjudicate`, `stage-memos`) run inside a copy of the flow directory |
| `tests/live/run_stage.py` | The isolation flows with real Claude workers and a minimal scripted supervisor |

### Validation per node, in order

| Node | Checks before its output is used |
|---|---|
| `extract_rules` (Opus, one branch per copy) | Flowstate schema + `_session_id` → `rate-spec-traced` gate → `worker-inputs-only` gate → `rules-agree` (behavioural agreement) → `rules-final` (second round if needed, cache write) |
| `adjudicate` (Sonnet, one branch per batch) | Schema + `_session_id` → `adjudications-grounded` gate → `worker-inputs-only` gate → `merge-adjudications` → Phase 5 `report.verify` |
| `write_memos` (Sonnet, one branch per batch) | Schema + `_session_id` → `memos-grounded` gate → `worker-inputs-only` gate → `render-memos` |

Gates and merge steps re-run the same checks, so a batch cannot pass on the branch and be altered later.

### Decisions and why

- **Two copies are two fan-out branches.** They run in parallel in separate sessions. Independence is
  verified, not requested: the audit gate reads each worker's transcript and fails if it opened anything
  other than its contract and clause index (for example the other copy's spec or a cached spec).
- **Agreement is by behaviour, not structure.** Careful readings name and order components differently.
  The live Sagar copies did exactly that (`weight_component` vs `freight_weight`) and were rightly judged
  equal. Probes use, per numeric field either spec reads: 0, every band end and constant, two points inside
  each interval, and two beyond the last. These are crossed with every service level and every combination
  of handling flags. Amounts are linear between thresholds, so agreement on these points is agreement
  everywhere, provided quantities combine a field with constants. Too many probes fail closed.
- **One bounded second round, copy against copy.** On disagreement two fresh copies must agree with each
  other; there is no majority vote with round 1. This keeps the approved rule (two independent extractions
  agree) inside an acyclic graph. If round 2 also disagrees, `rules-final` exits 1 and the run stops for a
  human with the differences on disk.
- **Every clause is accounted for.** Coverage requires each clause to be cited by a priced element, a gap,
  `non_pricing` or `unrepresentable`. Any `unrepresentable` entry stops the run, because a spec that leaves a
  pricing term out would under-state the contract.
- **Cache key exactly as approved.** The key is sha256(contract sha | format version | prompt version).
  Both versions are file hashes (schema; prompt + guide), so an edit cannot be forgotten. On every use a
  cached spec is re-traced against the contract, and it is reused only if it was extracted against the same
  shipment vocabulary and invoice charge codes (`extracted_with`); otherwise the contract is extracted again.
  The cache lives outside runs (`rules_cache_dir`); cache hits record which agreement produced the entry.
- **Adjudication cannot touch money.** Decisions have no amount fields. Packets carry the computed facts,
  the two policy options and full clause text. Code appends the cited clause ids to the justification, and
  `contract_clause` stays the clauses the expected amount comes from (the schema's meaning). This replaces
  the Phase 5 placeholder, which let an adjudication override `contract_clause`.
- **Memos: agents write prose, code writes facts.** Each memo has four prose fields; the table of amounts,
  disposition and clauses is rendered from the report. Memo ids are deterministic and rendering refuses a
  directory holding anything else.
- **Grounding rule.** Amounts marked as money must equal an amount in the packet (sign ignored). Other
  numbers must appear in the packet, except counting integers up to 12. Identifiers, dates, § references and
  #item references are not figures. Facts are collected generously, so faithful quotes pass.
- **Gate feedback is short.** Retry feedback shows a gate's last 10 stderr lines, so the CLI prints a header,
  at most 8 problems and a count.
- **Prompts carry their references.** The guide and the schema are included by flowstate, and workers get
  absolute input paths. The worked example is an invented contract whose figures do not match any real one.
  Workers load no skills (Phase 1 isolation); the graph decides what they see.
- **Isolation flows are test fixtures.** They live in `tests/stages/` and are copied next to the flow's real
  assets at run time, so the product directory holds no test graphs but the real prompts, schemas, gates and
  scripts run through flowstate.

### Found by the live runs and fixed

1. **Contracts condition charges on facts BlueFin does not record.** In `phase6-rules-live-1`, one Falcon
   copy correctly marked part of a residential-delivery clause `unrepresentable` (the condition was the
   consignee's address type, which shipments.json does not hold), so the run stopped. The other copy silently
   dropped that half of the condition. Stopping was correct, but it would have stopped every Falcon run.
   Fix: a condition leaf `{"unrecorded": "<fact>"}` with three-valued logic in `pricing.py`:
   - a component whose condition is unknown is excluded from the amount;
   - a billed charge that only such a component could justify is flagged `CHARGE_UNVERIFIABLE`;
   - a percentage of such a component is flagged `CONDITION_UNVERIFIABLE`, and its amount is undetermined;
   - both flags escalate (`policy.yml`);
   - agreement reports the status `unverifiable`, so a copy that drops the unrecorded part disagrees with one
     that keeps it.
2. **Copies disagreed on charge codes.** In the same run, Alpine's copies differed on `freight_incl_fuel`,
   which Alpine invoices never carry, and on the generic `handling` code. Fix, in two parts:
   - parsers declare `CHARGE_CODES`; each assignment lists the carrier's `invoice_charge_codes`, and agreement
     compares only those;
   - the guide states rules for combined, generic and specific codes.
3. **A relative cache path silently missed the cache** (`phase6-rules-live-3-cache`, operator error: script
   nodes run in the artefact directory). Fix: `rules.plan` refuses a relative cache directory, and the live
   runner resolves the path.

### Interpretations to review

- `CHARGE_UNVERIFIABLE` and `CONDITION_UNVERIFIABLE` escalate. The alternative would dispute until the
  carrier evidences the unrecorded fact.
- A generic invoice "handling fee" is checked against the contract's handling-type charges (for example
  protected handling of fragile goods). It is payable where the shipment qualifies and not applicable
  otherwise, rather than disputed outright as uncontracted.
- When copies agree, copy A is adopted. Its citations, gaps and non-pricing notes go into the spec; copy B's
  are kept in the agreement record.
- If round 2 still disagrees, the run stops for a human.

### Tests

193 freight tests pass without tokens (109 Phase 5 + 84 Phase 6). The 153 orchestrator tests are
unchanged, since nothing under `orchestrator/` changed.

| File | Tests | Coverage |
|---|---|---|
| `test_phase6_checks.py` | 32 | 14 tracing failures + carrier, equivalent encodings agree, 8 behavioural differences found, probe grid, probe limit, grounding, audit accepts/rejects/needs one worker |
| `test_phase6_rules.py` | 9 | Assignments, adopt + cache + reuse, prompt change and stale entry invalidate, second round agrees or blocks, invalid/duplicate/unrepresentable copies block, bad inputs incl. relative cache path, changed invoice codes invalidate |
| `test_phase6_packets.py` | 20 | Packet facts, 9 decision-check failures, merge into a verified report, empty merge, memo facts, 6 draft-check failures, rendering and refusals |
| `test_phase6_prompts.py` | 8 | Guide matches `vocab.py`, guide example is a valid spec, prompts render completely, isolation flows pass static validation |
| `test_phase6_unrecorded.py` | 10 | Three-valued conditions, unverifiable charge escalates, percentage of an unverifiable component, agreement on unrecorded, code-restricted comparison, parsers declare every code they emit on the real invoices |
| `test_phase6_stage_runs.py` | 5 | Through flowstate with fake workers: copies pass gates, agree, cache and are reused with no workers; an untraced spec fails the gate and a retry with the gate's feedback recovers; disagreement → second round → adopted; second round disagrees → `script_failed`; adjudication rejects an unoffered disposition then merges into a verified report; memos reject an invented figure then render |

### Live isolation runs (real Claude workers; `runs/_phase6-live/`, not committed)

| Run | Workers | Result | Cost |
|---|---|---|---|
| `phase6-rules-live-1` (real contracts, before fixes) | 6 Opus | 12/12 gates passed first attempt, 3 tool calls per worker, no audit violations. Sagar agreed (144 probes, 0 differences). Alpine disagreed on charge codes. Falcon had an `unrepresentable` term → `rules_agree` exited 1 (stop for a human) | $1.66 |
| `phase6-adjudicate-live-1` (synthetic acme packets) | 1 Sonnet | Both gates passed first attempt; 2 grounded decisions (escalate; accept where the billed amount matched and the only issue was the service level) | $0.21 |
| `phase6-memos-live-1` (synthetic acme report) | 1 Sonnet | 4 memos, both gates passed first attempt; memos state the issue, the clause basis and a concrete next step | $0.11 |
| `phase6-rules-live-2` (real contracts, after fixes) | 6 Opus | 12/12 gates passed first attempt. All three carriers agreed in round 1 (144 / 2,304 / 144 probes, 0 differences); both Falcon copies used `unrecorded` independently; both Alpine copies mapped `handling` to protected handling; 3 cache entries written | $1.53 |
| `phase6-rules-live-3-cache` (relative cache path) | 6 Opus | Cache missed (see fix 3); the third independent pair agreed in round 1 for all three carriers again | $1.50 |
| `phase6-rules-live-4-cache` | 0 | 3 cache hits, re-traced against the contracts and adopted; no workers | $0.00 |

Total live spend: about $5.01. The adjudication and memo live runs predate fixes 1–3, which do not touch
their prompts, checks or inputs. No real rate spec or run output is committed; the submission's evidence
comes from the final run in Phase 8.

### Known limitations

- **Supervision:** isolation runs use a scripted supervisor (retry gate/validation failures, stop otherwise),
  not the graph-orchestrator skill; that pairing is Phase 7.
- **Probe assumption:** agreement probes assume quantities combine a field with constants. A max/min of two
  different fields would bend along a diagonal the grid does not sample.
- **Grounding:** it catches invented or computed figures, not a wrong statement made with real figures;
  counting integers up to 12 are not checked.
- **Audit:** it sees tool calls in the transcript. The allowed tools are all visible there; a future tool
  that reads files without a tool call would not be.
- **Real data:** adjudication and memos have run on synthetic data only; real packets arrive with the July
  dry run.
- **Cache:** no locking or eviction. Concurrent runs writing the same key write identical content through
  atomic replace.
- **After a stop:** when extraction stops for a human (round 2 disagreement or `unrepresentable`), there is
  no tooling yet for a person to supply or approve a spec.
- **Flow digest:** it still does not cover the imported `freight` package (Phase 7).

---

## Phase 7 — reconciliation flow, reconcile-freight skill, July dry runs (2026-09-14)

### Scope and starting state

The plan's Phase 7 row: "Flow wiring, the reconcile-freight skill, a full July dry run, then fixes. Done when
the report validates", plus a proposed second run "to show numbers are identical across runs and to check how
much dispositions vary". The phase started from a clean tree at `2d97825`. Every Phase 5–6 building block
existed; there was no top-level DOT or flow.yml, no reconcile skill, and the flow digest did not cover the
`freight` package (a Phase 5 limitation).

### What was built

| Piece | Responsibility |
|---|---|
| `freight-reconciliation.dot` + `.flow.yml` | The full graph: 25 nodes (11 script, 4 agent, 4 fan-outs, 4 joins, start, done), gates on 6 edges |
| `freight/coverage.py` | `check_scope` (nothing unresolved, something in scope) and `check_priced` (every in-scope line priced exactly once, every line and finding decided or offered for judgement) |
| `freight/publish.py` | Re-verify the report and the run's memos, then publish `reconciliation-report.json` and `memos/` |
| `cli.py` | `check-scope`, `check-priced`, `publish`; `price --specs-json`; `assemble --manifest` (one row per in-scope line) |
| `scripts/`, `gates/` | `discover.sh`, `price.sh`, `assemble.sh`, `publish.sh`, `_paths.sh`; gates `scope-resolved.sh`, `priced-covers-scope.sh`; `rules-plan.sh` and `memo-plan.sh` resolve repository-relative paths |
| `definitions/` | `manifest`, `priced`, `publication`, and `reconciliation-report` (a copy of `report.schema.json`, kept identical by a test) |
| `orchestrator/lib/flowstate/loader.py` | `digest_include` in flow.yml: globs inside the flow directory join the flow digest |
| `.claude/skills/reconcile-freight/SKILL.md` | Entry skill: validate, init with only the inputs given, supervise with graph-orchestrator using a table of this flow's situations, report from `publication.json` |
| `reconcile.sh` | Headless launcher for a real orchestrator session with that skill |
| `tests/live/compare_runs.py` | Compare two completed runs |

### Decisions and why

- **Parsing is one script node, not the planned per-file fan-out (deviation).** Phase 3 recorded that every
  branch rewrites `state.yaml`. Parsing all 17 documents is cheap sequential code, so a fan-out would add
  orchestration cost that grows with the number of files for no gain. Discovery is gated by `scope-resolved`.
- **Assembly comes before memos** (the plan listed memos first). Memos are written from the verified report,
  so every memo's facts table shows exactly what is published.
- **Coverage gates after `discover` and `price`.** At volume the risk is a silently dropped or duplicated line.
  These gates sit on script nodes, so under graph-orchestrator's rules a failure pauses for a human.
- **Publish re-verifies instead of trusting upstream.** Before anything reaches its destination it checks:
  - the schema and every report invariant;
  - one row per in-scope line, counted from the manifest;
  - exactly one memo per non-accept row.

  It never deletes files it did not publish.
- **`digest_include` is a Flowstate change**, recorded for DESIGN.md. It covers `freight/**/*.py`,
  `config/*.yml` and `scripts/_paths.sh`, so editing pricing code, policy or carrier config mid-run gives
  `flow_changed`, which both skills already treat as a pause.
- **Inputs default to repository files, and relative paths are resolved against the repository root**
  (`_paths.sh`). This is the lesson of the Phase 6 relative cache path. `init --var period=2026-07` is a
  complete run.
- **The report is validated against the fixed contract twice.** Flowstate validates the `assemble` output
  against the flow's copy of the schema (definitions must live in the flow directory; a test keeps the copy
  identical). `assemble` and `publish` also validate against `report.schema.json` itself.
- **The skill separates domain meaning from generic supervision.** graph-orchestrator keeps the decision
  procedure; reconcile-freight only says what this flow's situations mean. For example:
  - a failed tracing gate → retry;
  - a failed audit gate → respawn, since a retry cannot remove what the transcript shows;
  - an extraction that cannot be adopted → pause.

  It forbids the orchestrator to suggest dispositions, amounts, clause readings or memo text, and its final
  report copies figures from `publication.json`.
- **The launcher mirrors the Phase 4 live drill.** Bash is limited to `orchestrator/bin/flowstate`, plus
  Read, Grep, Glob and Skill. There is no Write or Edit, and the inherited session variables are removed.
- **Dry runs publish into `runs/_phase7-dry/`, not the repository root.** The deliverables come from Phase 8.

### Dry run 1 (`runs/_phase7-dry/july-dry-1`, not committed)

- **Orchestrator (Sonnet):** loaded both skills with the Skill tool; ran validate, init with `period` and
  `publish_dir` only, and one `advance`, which completed; then read `publication.json`. 9 turns, $0.25, no
  permission denials, no Write or Edit.
- **Run:** 2.5 minutes, 8 workers (6 Opus extraction, 1 adjudication batch, 1 memo batch), $1.77. 18 gates
  passed; no retries, respawns or pauses.
- **Extraction:** all three carriers agreed in round 1 (144 / 2,304 / 144 probes); 3 cache entries written.
- **Pricing:** 127 in-scope lines. Policy decided 126; one line was left open for adjudication (an invoice
  line whose own charges do not add up to its total). One invoice finding.
- **Published:** 122 accept, 4 dispute, 1 escalate; 1 escalated invoice finding; 6 memos. The report was
  verified at `assemble` and again at `publish`.
- **What was checked:** engine behaviour against the approved rules, not against any expected answer.
  - Every flagged line's disposition followed from its flags as `policy.yml` prescribes.
  - Every parser's attribute keys match the attribute-mismatch check, so a dispute with no explaining flag is
    a genuine amount difference, not a parser gap.

### Found in dry run 1 and fixed

1. **Published output depended on names an extraction worker chose.**
   - Finding ids and memo file names embedded the spec's adjustment name (`...-volume_discount`), and its
     description showed it ("Volume_discount applies...").
   - Gap messages embedded component names.
   - Two agreeing extractions could therefore publish different ids and text for identical money.

   Fix: adjustments are described and identified by what they do (`pricing.adjustment_terms`, e.g.
   `discount-5pct-from-13`, with count conditions stated as inclusive bounds), and flags name the charge's
   kind. `test_phase7_determinism.py` shows a renamed but equivalent spec produces identical text and ids.
2. **A duplicate billing cited its first billing's clauses** as `contract_clause`, although its expected 0
   comes from the duplicate rule. The memo writer then claimed those clauses say a consignment is billed once.
   Fix: a duplicate cites no clause.
3. **A memo named an invoice column** (`freight_rs`). Fix: the memo style guide requires plain words for charges.

### Tests

- **Freight:** 207 pass without tokens (193 through Phase 6 + 14 new).
  - `test_phase7_flow.py` (2): the real flow end to end through Flowstate with fake workers on synthetic acme
    data, covering every gate, each disposition path and publication; plus the digest's coverage.
  - `test_phase7_wiring.py` (5): coverage checks, publish success and its four refusals, the spec map, the
    schema copy.
  - `test_phase7_skill.py` (4): the skill names only real commands, flags, nodes, gates, situations, variables
    and artefact paths, and keeps the orchestrator out of the reconciliation.
  - `test_phase7_determinism.py` (3).
- **Orchestrator:** 159 pass (153 + 6 in `test_flowstate_digest.py`). One pre-existing Phase 3 test,
  `test_fork_runs_branches_in_parallel_and_merges_by_name`, asserts wall time under 4 s. It failed once while a
  live run and the freight suite loaded the machine, and passed alone in 4 s: a timing assumption, not a
  regression. After the live runs, on an idle machine, all 159 pass.

### Runs 2 and 3: after the fixes, concurrent, independent extractions

Both used `use_rules_cache=false`, so each extracted all three contracts afresh and neither read nor wrote the
cache.

- **Flows:** both completed and published. Each had 8 workers, all exiting successfully, and passed 18 gates
  with no retries, respawns or pauses, in about 2.5–3 minutes. Workers cost $1.77 (run 2) and $1.75 (run 3).
- **Orchestrators:** each loaded both skills, validated, initialised with only `period`, `publish_dir` and
  `use_rules_cache=false`, and advanced once to `completed`. The account's session limit then ended both
  sessions before their final report: run 2 right after the completing `advance`, run 3 after reading
  `publication.json` and checking the events. The launcher exited 1; the runs were unaffected, since every
  worker had finished. `reconcile.sh` now says explicitly when a run completed but its orchestrator session
  ended early.
- **Run 2 vs run 3** (`compare_runs.py`):
  - the three carriers' adopted rate specs price identically (144 / 2,304 / 144 probes);
  - every line's billed, expected, delta and contract clause, every finding, every invoice total and the
    summary are identical;
  - the one adjudicated line was decided the same way, citing the same clauses;
  - the same six memo files were published, with identical facts tables. Memo prose differs in wording only
    (headlines, summaries, next steps), with the same substance.
- **Run 1 vs run 2:** the specs are identical. The only differences are the fixes' intended ones: the duplicate
  line's `contract_clause` is now null, and the finding's memo file name is stable.
- **Found and fixed:** run 3's adjudication justification, which is published verbatim in the report, quoted
  CSV column names (`freight_rs`, `chill_prem_rs`). The adjudication prompt now carries the same plain-words
  rule as the memo guide. This has not been exercised live yet: the session limit prevented another run.

Live spend for the three dry runs: about $5.29 in workers and $0.59 in orchestrator sessions.

### Done-when

"The report validates": yes, in all three runs. Flowstate validated the `assemble` output against the schema,
`assemble` verified every invariant including one row per in-scope line, and `publish` verified it all again.

### Known limitations

- **Orchestrator reports:** only run 1 has the skill's final report end to end; runs 2 and 3 lost theirs to
  the account session limit.
- **Small sample:** variance is measured over three live runs, and the July data leaves little for agents to
  decide (one adjudicated line, six memos). Agreement across independent extractions was perfect in all
  three runs, but three runs cannot bound how often extraction would need its second round.
- **Unquantifiable discount:** the Alpine volume discount cannot be quantified whenever any Alpine line is
  undetermined (here, one line at an exact band boundary). This also makes `total_expected` null. It fails
  closed by design (a Phase 5 interpretation), at the cost of an escalation for the whole discount.
- **Launcher exit code:** it reflects the orchestrator session, not the run.
- **Scale:** discovery and pricing are linear and sequential but not measured beyond 17 documents and 127 lines.
- **Evidence:** the dry-run outputs are not committed; Phase 8 produces the deliverables and evidence.

---

## Phase 8 — final run, evidence, DESIGN.md (2026-09-15)

### Scope and starting state

The plan's Phase 8 row: "Final clean run, commit evidence, DESIGN.md, CLAUDE.md — deliverables complete." The
tree was clean at `ac5485a`, the flow validated, the account session limit that ended two Phase 7
orchestrator sessions had reset, and there were no deliverables at the repository root.

### The final run

- **Command:** `FRESH_EXTRACTION=1 RUN_ID=freight-2026-07 factory/flows/freight-reconciliation/reconcile.sh
  2026-07`, with the default runs directory (`runs/`) and the default publish directory (the repository root).
  Fresh extraction means the submitted evidence shows every contract being read, not a cache hit.
- **Orchestrator (Sonnet, 13 turns, $0.26):** loaded both skills; ran validate, init (`period=2026-07`,
  `use_rules_cache=false`) and one `advance`, which completed; read `publication.json`; reported correctly.
  - One Bash call was denied: `python3 -c` reading the published report's summary. This is a shell viewer the
    skills forbid, and the launcher's `dontAsk` mode refused it. The orchestrator continued with Read and Grep.
- **Run:** 2 min 22 s. 8 workers, all exited without error, $1.75. 24 node completions, 18 gates passed; no
  retries, respawns or pauses. All three carriers agreed in round 1 (144 / 2,304 / 144 probes, no
  differences), with no cache reads or writes.
- **Published:** 127 lines (122 accept, 4 dispute, 1 escalate), 1 escalated invoice finding, ₹8,832 in dispute,
  `total_expected` null, 6 memos.
- **Checked after the run (read-only):**
  - the report validates against `report.schema.json`;
  - there is one memo per non-accept item;
  - the published report and memos are byte-identical to the run's artefacts, and the SHA-256 matches
    `publication.json`;
  - `compare_runs.py` against `july-dry-2`, an independent extraction: specs price identically, and amounts,
    findings, totals, dispositions and memo files are identical.
- **The Phase 7 adjudication wording fix, first live exercise:** the justification names "the freight charge",
  and no published prose contains column or code names. The adjudicated line kept its disposition but cited
  only §1 (the dry runs cited §1 and §3).

### Evidence committed

- `reconciliation-report.json` and `memos/`, as published by the run.
- `runs/freight-2026-07/` verbatim (291 files, 2.6 MB).
- `runs/freight-2026-07.orchestrator/`, plus `launcher.log` (the launcher's console output, moved there from
  `runs/`).
- **Checked before committing:** no credential patterns and no email addresses; recorded environments hold only
  `FLOWSTATE_*` and `AGENTCTL_*` variables; no file over 500 KB. Absolute paths to the local checkout remain in
  the run records, as part of what happened.

### Documentation

- `DESIGN.md`, distilled from this log: how to run it, the submitted run, architecture, validation layers,
  judgement calls, changes to the kit, and post-implementation notes.
- `CLAUDE.md`: the deliverables and the committed evidence, which must never be edited by hand.

### Known limitations of the finished system

- **Human approval:** nothing yet lets a person approve a contract reading into the cache after extraction stops.
- **Duplicate detection:** it re-reads every month's documents; a persistent ledger of billed consignments
  would scale better.
- **Branch scaling:** Flowstate rewrites `state.yaml` on every change; behaviour with hundreds of branches is
  unmeasured.
- **Volume discounts:** unquantifiable whenever any line of the invoice is undetermined.
- **Performance:** unmeasured beyond one month's 127 lines.
- **Orchestrator rules:** they are followed imperfectly and hold because of launcher permissions. The
  interactive `/reconcile-freight` path relies on the user's own permission settings for the same guarantee.
- **Timing-sensitive test:** one pre-existing Flowstate test asserts wall time.
