# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A take-home exercise (see `PROBLEM.md`) to build an agent system that reconciles carrier freight
invoices against BlueFin Commerce's shipment records and rate contracts, producing
`reconciliation-report.json` (validated by `report.schema.json`) and a `memos/` directory of
human-readable memos for every non-`accept` line/finding.

Read `PROBLEM.md` first — it defines the deliverables and ground rules. Read `brief.md` for the
conceptual background on why this is architected as a graph rather than a single freewheeling
agent (context-window limits, instruction decay, run-to-run variance — this pipeline moves money,
so every run has to hold up, not just a lucky one).

**Ground rules that constrain any implementation work here:**
- The reconciliation must be produced by the agent system itself, from a real run — never hand-solved
  and pasted into the output files.
- Design for volumes well beyond the 5-invoice sample (more carriers, more invoices, more lines).
- `report.schema.json` is the only fixed contract. Everything upstream (architecture, intermediate
  formats, validation) is an open design decision to be recorded in `DESIGN.md`.

## Current state of the kit

The kit shipped as a **partial scaffold**. Work is proceeding in phases (Option A: build flowstate +
agentctl, then run the reconciliation as a flowstate graph); `IMPLEMENTATION_LOG.md` records each
phase's decisions and is the source for `DESIGN.md`.
- `orchestrator/lib/agentctl/` — **implemented (Phase 1)**, see "agentctl" below.
- `orchestrator/lib/flowstate/` — **implemented through Phase 3**, see "flowstate" below: acyclic
  graphs with start/done, agent and script nodes, gates, conditions, `fork`, `join` (with reducers)
  and `dynamic_fanout`. Nested fork/fan-out regions are rejected.
- `.claude/skills/graph-orchestrator/` — **implemented (Phase 4)**, see "graph-orchestrator skill"
  below.
- `factory/flows/freight-reconciliation/` — **deterministic code layer implemented (Phase 5)**, see
  "Freight code layer" below. No DOT/flow.yml, agent prompts or reconcile skill yet (Phases 6–7), and
  no real rate specs: those are produced by agent nodes in Phase 6.
- Kit shell files were committed without the executable bit. Both `bin/` wrappers are fixed; flowstate
  runs flow scripts, gates and reducers through their `#!` interpreter, so the rest work unchanged.
- `smoke-branch/scripts/reduce.sh` was missing from the kit and has been added as a minimal fixture.
  `factory/flows/smoke-fanout/` is a new demo flow for `dynamic_fanout` (the kit had none).
- `factory/factory-prefs.yml` (gitignored) is created from `factory-prefs-example.yml` on first use.

## Setup and commands

```bash
orchestrator/setup.sh        # one-time: creates orchestrator/.venv, installs pyyaml/jsonschema/pydot
                              # requires python3, git, tmux, jq on PATH
orchestrator/bin/flowstate   # graph runtime CLI — `python -m flowstate`
orchestrator/bin/agentctl    # worker lifecycle CLI (spawn/wait/send/kill) — `python -m agentctl`
```

Tests (pytest; install with `orchestrator/.venv/bin/pip install -r orchestrator/requirements-dev.txt`):

```bash
(cd orchestrator && .venv/bin/python -m pytest)                                   # all; no tokens spent
(cd orchestrator && .venv/bin/python -m pytest tests/test_flowstate_parallel.py -k empty)  # single test
orchestrator/tests/live/smoke_claude_worker.sh        # real Claude worker via tmux (spends cents)
orchestrator/.venv/bin/python orchestrator/tests/live/probe_isolation.py  # re-verify worker isolation
SCENARIO=recover orchestrator/tests/live/orchestrate_drill.sh   # real Claude orchestrator + skill (~$0.20)
SCENARIO=pause orchestrator/tests/live/orchestrate_drill.sh

# freight code layer (separate suite, same venv)
(cd factory/flows/freight-reconciliation && ../../../orchestrator/.venv/bin/python -m pytest)
PYTHONPATH=factory/flows/freight-reconciliation orchestrator/.venv/bin/python -m freight --help
```

Lifecycle and agent-node tests need a working `tmux`; several flow scripts need `jq`. Scratch runs go
to `runs/_*/` (gitignored).

Run the demo flows (fake harness = deterministic scripted workers, no tokens; drop the `--harness`
and `--fake-script` flags to use real Claude workers; smoke-branch has no agent nodes):

```bash
F=orchestrator/tests/fixtures/fake
orchestrator/bin/flowstate --runs-dir runs/_scratch init smoke-test --var research_topic="tides" \
  --harness fake --fake-script research=$F/smoke-test/research.json --fake-script summarise=$F/smoke-test/summarise.json
orchestrator/bin/flowstate --runs-dir runs/_scratch init smoke-branch
orchestrator/bin/flowstate --runs-dir runs/_scratch init smoke-fanout --var item_names="alpha,beta,gamma" \
  --harness fake --fake-script describe=$F/smoke-fanout/describe.json
orchestrator/bin/flowstate --runs-dir runs/_scratch advance <run_id>
```

## agentctl

`agentctl --registry <dir> {spawn,wait,send,kill,status,list,logs}`; JSON on stdout (except `logs`).
- The registry is a directory inside a run (`runs/<run_id>/workers/<worker_id>/`): `meta.json`,
  `prompt.md`, aggregate `transcript.jsonl`/`stderr.log`, and `invocations/NNN/` (000 = spawn,
  001+ = `send`) holding `command.json`, `transcript.jsonl`, `stderr.log`, pids, `result.json` and
  `exit_code`. `exit_code` is written last and is the completion marker.
- Each invocation runs `python -m agentctl.runner <invocation_dir>` inside a detached tmux session
  (`agentctl-<registry-hash>-<id>`). The runner strips `CLAUDE*` session variables inherited from
  the launching Claude Code session (keeps provider/config vars), tees stream-json output, and
  forwards SIGHUP/SIGTERM to the child's process group.
- States: running, stalled (transcript unchanged for `stall_after_s`), exited, failed, killed, lost.
- Harnesses (`harnesses/`): `claude` builds `claude -p --output-format stream-json` with
  `--session-id` on spawn and `--resume` on send; `fake` runs `fake_worker.py` from a JSON script
  (captures from the prompt, writes files, sleeps/heartbeats, fixed exit codes; any step can be made
  conditional on the prompt with `when`/`unless` regexes) for token-free tests.
- Worker isolation is enforced in `harnesses/claude.py` and was verified by experiment, not assumed:
  tools limited to Read/Write/Edit/Glob/Grep (`--tools` + `--allowedTools`, Bash/web disallowed),
  `--setting-sources ""` (no project CLAUDE.md/skills), `--disable-slash-commands` (bundled skills),
  `--safe-mode` (auto-memory), `--strict-mcp-config`, `--permission-prompts none`. Dropping any of
  these re-opens a leak that `probe_isolation.py` detects.

## flowstate

`flowstate [--runs-dir DIR] {validate,init,advance,retry,respawn,pause,resume,abort,status,events}`;
JSON on stdout. Situations (including failures) exit 0; command errors exit 1.

Module map: `loader.py`/`validate.py` (static checks, regions), `engine.py` (init, top-level cursor,
decisions, status), `execution.py` (one agent/script node inside a `Scope`, as non-blocking steps),
`parallel.py` (fork/fan-out/join/reducers), `scope.py`, `script_runner.py` (detached script runs),
`state.py`, `runtime.py` (situations, feedback), `outputs.py`, `gates.py`, `conditions.py`.

- **Loading**: pydot parses the DOT (a `graph [...]` statement shows up as a pseudo-node named
  `graph`; values keep their quotes). Node kind comes from the existing syntax: `Mdiamond` start,
  `Msquare` done, `prompt_template` agent, `runner=script|fork|join|dynamic_fanout`. All issues are
  collected before any run exists: attribute whitelists per kind, referenced files, schema validity,
  one start/done, acyclic, reachability, conditions on every non-fork branch, region rules (below),
  and a dataflow pass that every `{var}` / `$FLOWSTATE_VAR_x` is declared *and* set on every path.
- **Run directory** `runs/<run_id>/`: `state.yaml` (authoritative; only mutated in
  `RunStore.transaction()` under `.state.lock`), `events.jsonl` (append-only, written after the
  state change), `artefacts/` (branch outputs under `artefacts/branches/<branch_id>/`), `workers/`
  (the agentctl registry), `logs/` (script runs, gate evidence, rejected-output snapshots; branch
  logs under `logs/branches/<branch_id>/`, reducer runs under `logs/joins/<join>/`). `advance`/
  `retry`/`respawn` take a non-blocking `.advance.lock` lease; `pause`/`abort`/`status` do not.
  The flow digest recorded at init must still match, or `advance` returns `flow_changed`.
- **Variables**: run inputs via `init --var` (lists/dicts as JSON), builtins (`_run_id`, `_run_dir`,
  `_run_artefact_dir`, `_flow_dir`, `_node_id`, `_session_id` for agents; inside regions also
  `_branch_id`, `_branch_index`, `_parallel_node`, and `item` for dynamic_fanout), and
  `sets_variables` (a file name binds the file's path; `{file, pointer}` binds a JSON-pointer value).
  A node's own path bindings are pre-bound before it runs, so a prompt can say "write to
  `{research_brief}`"; they are committed only after validation. Missing variables are errors.
- **Agent nodes**: prompt rendered + a runtime footer stating the assigned session id; spawned
  through `agentctl.lifecycle` with a fresh UUID. Every JSON output must carry that exact
  `_session_id`. **Script nodes** (and reducers) are launched detached via `script_runner.py`
  (runs the `#!` interpreter with `FLOWSTATE_VAR_*`, writes `<stem>.exit_code` last), so they run in
  parallel and survive `advance` being killed. `command.json` records only `FLOWSTATE_*` env.
- **Advancing a node**: outputs exist → JSON Schema valid → `_session_id` (agents) → bindings →
  conditions pick exactly one edge → that edge's gates exit 0 → one atomic commit. Any failure is a
  persisted situation and nothing is committed.
- **Situations** (`runtime.py`): persisted until acted on — `validation_failed`, `gate_failed`,
  `worker_failed`, `script_failed`, `node_interrupted`, `render_failed`, `condition_error`,
  `no_route`, `ambiguous_route`, `fanout_invalid`, `reducer_failed`, `reducer_output_invalid`,
  `reducer_interrupted`, `retries_exhausted`; transient — `worker_running` / `script_running` /
  `branches_running` (`--max-wait`), `worker_stalled`, `worker_timeout`, `busy`, `flow_changed`;
  run-level — `paused`, `aborted`, `completed`. Branch situations add `branch_id`, `branch_index`,
  `parallel_node`, `item`, and `branches` counts.
- **Decisions**: `retry` sends structured feedback into the same worker conversation (script nodes
  rerun; `retry <join>` reruns the failed reducer fold); `respawn` starts `<worker>.respawn-N` with a
  new session. For nodes inside a region pass `--branch <id>` (inferred when exactly one branch
  matches). Both consume the node's `max_retries` budget. `pause` stops advancement without killing
  workers; `abort` kills agent workers, detached scripts and reducers. `pause_at=optional` pauses
  only when supervision is `high`.
- **Recovery**: an agent attempt is recorded (`launch: pending`) before agentctl is called; detached
  scripts are observed through their exit marker. A new `advance` process reconnects to running
  workers/scripts instead of starting them again. Completed nodes and branches never rerun; a
  runner that vanished without a result is `node_interrupted` / `reducer_interrupted`.

### Parallel regions (fork, join, dynamic_fanout)

- **Regions (static)**: each `fork`/`dynamic_fanout` owns the nodes between it and exactly one
  `join`. Branches cannot leave the region, be entered from outside it, share nodes (fork), or contain
  another fork/fan-out. Edges leaving the parallel node carry no conditions or gates. Fork branches
  must produce disjoint variables; region variables cannot also be produced outside the region.
  `dynamic_fanout` has exactly one outgoing edge (the template) and needs `items=<variable>` (a
  `list`, or a `path` to a JSON list file); optional `max_items` (default prefs `max_fanout_items`,
  500) and `max_parallel` (default prefs `max_parallel_branches`, 4). `join` takes optional
  `reducer_script` + `summary_var` (a `dict` variable) — both or neither.
- **Branches**: created once, in one write, when the cursor reaches the parallel node. Fork: one per
  outgoing edge, ids `<fork>-<entry>` in sorted entry order. Fan-out: one per item, ids
  `<fanout>-0000`, `-0001`, … by list position (width grows past 10 000). Each branch persists its
  item and a context in which `_run_artefact_dir` is `artefacts/branches/<branch_id>/`; agent worker
  ids are `<node>.<branch_id>`.
- **Execution**: one `advance` process steps every branch without blocking; at most `max_parallel`
  branches are running at once. Agent workers (tmux) and scripts (detached) execute concurrently.
  Sibling branches keep running when one fails; `advance` returns the first branch situation in
  branch order once no other branch can make progress (or at `--max-wait`).
- **Join**: completes only when every branch has reached it and every fold is done. Merge: fork
  branch variables by name; fan-out region variables become lists in branch order. The reducer runs
  once per branch, strictly in branch order (branch *k* is folded as soon as it and all earlier
  branches have arrived), reading `FLOWSTATE_REDUCER_SUMMARY_IN` (`{}` initially),
  `FLOWSTATE_REDUCER_BRANCH_VARS`, the branch's `FLOWSTATE_VAR_*` and `FLOWSTATE_BRANCH_ID`, and writing
  a JSON object to `FLOWSTATE_REDUCER_SUMMARY_OUT`; the result becomes `summary_var`. Completing the
  join, the parallel record and the cursor move is one write.
- **Empty fan-out**: no branches; the join completes immediately with region variables `[]` and
  `summary_var` `{}`.
- **State** under `state.parallel.<parallel_node>`: `kind`, `join`, `status` (running | merged |
  completed), `items`, `max_parallel`, `branch_order`, `branches.<id>` (`index`, `entry`, `status`
  pending | running | awaiting_decision | completed, `cursor`, `context`, `variables`, `nodes` with
  Phase 2 node records, `situation`), and `join_state` (`status`, `folded`, `summary`,
  `reducer_runs`, `merged_variables`). Top-level `nodes.<region node>` is only a
  `status: in_branches` marker. `flowstate status` summarises all of this under `parallel`.

## graph-orchestrator skill

`.claude/skills/graph-orchestrator/SKILL.md` (procedure) + `situations.md` (reference) tell an
orchestrator agent how to supervise any Flowstate run. Division of labour: the graph states the
guarantees, Flowstate and agentctl do the mechanics, the orchestrator only judges what a situation
means and picks the next Flowstate command. It contains no domain logic.

- **Loop**: `flowstate advance RUN --max-wait 300` → read the situation → inspect the evidence it
  points to → one Flowstate command (`retry`, `respawn`, `pause`, `resume`, `abort`) → advance again.
  Stops on `completed`, `aborted`, `paused`, or after pausing for a human.
- **Decision order**: finished/waiting → budget spent (`retries_remaining` = 0) → definition, input or
  routing problems (pause) → worker/output problems (retry, respawn or pause) → deterministic code
  failures (pause unless the cause is transient) → unknown outcome (retry once) → anything unclear
  (pause). The skill prefers `pause` to `abort`, which it never does on its own initiative.
- **Retry vs respawn**: retry continues the same worker conversation with precise, evidence-based
  feedback (JSON pointer, violated constraint, value received, file to rewrite; never supplied
  content); respawn starts `<worker>.respawn-N` with a new session. Stalled workers cannot be retried
  (Flowstate returns `worker_busy`): wait once with `--stall-after`, then respawn. Both share the node's
  budget, which node situations report as `max_retries` / `retries_remaining`.
- **Branches**: pass `--branch <branch_id>` from the situation (except reducer situations, which are on
  the join); never touch completed or running branches; several branches failing alike → pause.
- **Forbidden**: editing anything under a run directory or a flow definition, writing outputs,
  starting/killing workers directly (claude, agentctl spawn/send/kill, tmux, kill), a second driver,
  keeping a private retry counter. One plain `flowstate` command per shell call, no shell expansion.
- **Tests**: `tests/test_orchestrator_skill.py` checks the skill against the real CLI/runtime (every
  command, flag, situation and error code) and runs a scripted `Supervisor` that follows the decision
  procedure through the real CLI against `tests/fixtures/flows/orchestrator-drill/` (fake workers).
  `tests/live/orchestrate_drill.sh` runs a real Claude orchestrator that loads the skill with the Skill
  tool, with Bash pre-approved only for `orchestrator/bin/flowstate` in `dontAsk` mode (mutating shell
  commands are denied; read-only ones still run).

## Freight code layer

`factory/flows/freight-reconciliation/`: `freight/` (Python package), `config/carriers.yml` (carrier →
contract, consignment prefix, invoice formats), `config/policy.yml` (flag → disposition effects),
`definitions/rate-spec.json`, `tests/`. CLI for script nodes: `python -m freight {discover, clauses,
check-spec, price, assemble}` (JSON out; errors exit 1).

- **Money** (`money.py`): Decimal everywhere, decimal strings in JSON, half-up to the paisa once per
  line total. Tolerance lives in policy.yml.
- **Parsers** (`parsers/`): one strict adapter per format (`alpine-json`, `falcon-text`, `sagar-csv`) →
  normalized documents (lines, charges mapped to `vocab.CHARGE_CODES`, invoice-stated attributes kept
  as evidence only, stated totals, integrity facts). Unknown text or formats raise `ParseError`.
  Sagar invoices have no id in the file: id from the file name, period from booking dates.
- **Discovery** (`documents.py`): parses every file; scope = invoices whose billing period is the run's
  period + credit notes correcting them; everything else is `reference` (used for cross-period
  duplicate detection) or `unresolved`, each with a reason.
- **Clause index** (`contracts.py`): numbered clauses, sections, agreement ref, term dates and the
  numbers in each clause — for citations and Phase 6 grounding. It never interprets prices.
- **Rate spec** (`ratespec.py`, `definitions/rate-spec.json`): pricing rules as data — quantities
  (`max`/`min`), components (`flat`, `per_unit`, `banded_rate`, `percent_of` earlier components) with
  shipment conditions and the charge codes they account for, allowed service levels, invoice
  discounts gated on consignments in the billing month, term, gaps. Bands keep the contract's
  inclusive/exclusive ends; values in no band are never snapped. `pricing_view()` strips citations
  and wording for comparing two independent extractions.
- **Pricing** (`pricing.py`): prices from `shipments.json` + spec only; flags: `NO_SHIPMENT_MATCH`,
  `SHIPMENT_AMBIGUOUS`, `SHIPMENT_DATA_MISSING`, `CARRIER_MISMATCH`, `OUTSIDE_TERM`, `CONTRACT_GAP`,
  `OUTSIDE_RATE_CARD`, `UNRECOGNIZED_CHARGE`, `UNCONTRACTED_CHARGE`, `CHARGE_NOT_APPLICABLE`,
  `DUPLICATE_BILLING` (earliest billing across all periods wins; later ones expect 0),
  `SERVICE_NOT_OFFERED`, `NOT_DELIVERED`, `COMPONENT_ARITHMETIC`, `ATTRIBUTE_MISMATCH`, `UNDERBILLED`,
  credit-note flags. Credit line expected = original expected − original billed − earlier credits;
  the original is flagged `CREDIT_NOTE_OFFSET`. Invoice findings: `ADJUSTMENT_MISMATCH`,
  `ADJUSTMENT_UNDETERMINED`, `INVOICE_TOTAL_MISMATCH`.
- **Policy** (`policy.py`): effects escalate > offset > dispute > base (dispute if billed − expected >
  tolerance, else accept); a `judgement` flag turns the result into `[outcome, escalate]` for
  adjudication. Unknown flags or missing policy entries fail closed.
- **Report** (`report.py`): dispositions only from policy or from adjudications restricted to the
  offered options; deterministic justifications with clause citations; `verify()` enforces
  `report.schema.json`, one row per in-scope line, delta = billed − expected, recomputed totals,
  no disputed line offset by a credit note (each rupee counted once), and policy-allowed dispositions.
- **Tests**: real data only for structure and the scope rule; every pricing expectation uses the
  synthetic `acme` carrier in `tests/synthetic.py`.

## Flow file architecture (DOT + flow.yml)

A flow is two files per directory, `factory/flows/<name>/<name>.dot` and
`factory/flows/<name>/<name>.flow.yml`, plus supporting `prompts/`, `scripts/`, `gates/`,
`definitions/` subdirs:

- **`.dot`** (Graphviz digraph) declares nodes and edges. Node attributes select a runner and its
  config: agent nodes (`shape=box` with `prompt_template`, `output_schema`, `model`,
  `permission_mode`, `pause_at`, `working_dir`) get a fresh worker session per node with a rendered
  prompt; `runner=script` nodes run a deterministic script instead of an LLM; `runner=fork`,
  `runner=dynamic_fanout` (`items`, `max_items`, `max_parallel`) and `runner=join` (`reducer_script`,
  `summary_var`) handle parallel fan-out/fan-in. Edges may carry `gates` (comma-separated scripts that
  must `exit 0`) and a `condition` (`count > 0 and status == "ok"`; operators `== != < <= > >=`,
  `and`/`or`, no parentheses).
- **`.flow.yml`** declares `output_schemas` (mapping a node's `output_schema` name to the files it
  must produce, each validated against a JSON Schema in `definitions/`, and which flow `variables`
  those files populate) and the typed `variables` themselves (`string`, `path`, `number`,
  `integer`, `boolean`, `dict`, `list`, `any`; optional `default`, `required`). `item` is reserved.
- Variables populated by one node are exposed to scripts/gates downstream as
  `FLOWSTATE_VAR_<name>` environment variables (lists/dicts as JSON).
- `working_dir="{_run_artefact_dir}"` — the per-run (or per-branch) artefact directory.

Demo flows:
- `factory/flows/smoke-test/` — linear agent-node flow (`research` → `summarise`) with a
  schema-validated output at each step and a gate (`gates/brief-exists.sh`) between them.
- `factory/flows/smoke-branch/` — `fork` → `worker_a`/`worker_b` (script nodes) → `join` with a
  reducer maintaining `branch_summary` → `merge_notes` reading both branches' notes.
- `factory/flows/smoke-fanout/` — `list_items` → `dynamic_fanout` → per item `describe` (agent) →
  gate → `stamp` (script) → `join` with a reducer → `report` over the merged list.

## Reconciliation data

- `data/shipments.json` — BlueFin's own shipment records; ground truth for what was actually
  shipped. Keyed by `shipment_id`, cross-referenced from invoices via `carrier_consignment_ref`.
- `data/contracts/*.md` — one prose rate contract per carrier (alpine-express, falcon-freight,
  sagar-roadlines). **The contracts are the sole authority on what anything should cost** — expected
  amounts and dispute/escalate justifications must trace back to a specific clause
  (`contract_clause` field in the report schema).
- `data/invoices/` — 17 documents for July–September 2026 in three native formats: Alpine as JSON
  (`ALPINE-*.json`), Falcon as fixed-layout text (`FALCON-*.txt`, fortnightly), Sagar as CSV
  (`SAGAR-*.csv`, fortnightly). The five July invoices (`ALPINE-0726`, `FALCON-2026-07A/B`,
  `SAGAR-JUL-1/2`) plus `FALCON-CN-01` (credit note against `FALCON-2026-07A`) are the July scope;
  `SAGAR-CN-01` corrects an August invoice and is out of scope. Other months are reference data.

## Output contract (`report.schema.json`)

- Every invoice line in every invoice file must appear exactly once in `lines`, including unmatched
  ones (`shipment_id: null`).
- `disposition` is one of `accept` / `dispute` / `escalate`; every non-`accept` line or
  `invoice_findings` entry needs a corresponding memo in `memos/`.
- `delta = billed_amount - expected_amount`; `expected_amount`/`delta` are `null` only when
  genuinely uncomputable.
- `summary.total_in_dispute` sums absolute deltas across disputed lines *and* invoice-level
  findings with no double counting — a rupee attributed to an invoice-level finding must not also be
  counted via a line, or vice versa.
