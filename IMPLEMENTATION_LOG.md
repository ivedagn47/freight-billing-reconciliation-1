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
