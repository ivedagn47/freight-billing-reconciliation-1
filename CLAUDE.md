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
- `factory/flows/freight-reconciliation/` — **implemented through Phase 7**: the deterministic code layer
  (Phase 5), the agent nodes (Phase 6), and the wired `freight-reconciliation` flow with the
  `.claude/skills/reconcile-freight/` entry skill (Phase 7); see "Freight code layer", "Freight agent nodes"
  and "Reconciliation flow" below. The final run, committed evidence and DESIGN.md are Phase 8.
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

# Phase 6 agent nodes in isolation with real Claude workers (spends tokens; output under runs/_phase6-live/)
orchestrator/.venv/bin/python factory/flows/freight-reconciliation/tests/live/run_stage.py rules       # Opus x6, real contracts
orchestrator/.venv/bin/python factory/flows/freight-reconciliation/tests/live/run_stage.py adjudicate  # Sonnet, synthetic packets
orchestrator/.venv/bin/python factory/flows/freight-reconciliation/tests/live/run_stage.py memos       # Sonnet, synthetic report

# Reconcile a period with real workers (spends tokens). Interactive equivalent: /reconcile-freight 2026-07
factory/flows/freight-reconciliation/reconcile.sh 2026-07                                   # publishes to the repo root
RUNS_DIR=runs/_dry PUBLISH_DIR=runs/_dry/published factory/flows/freight-reconciliation/reconcile.sh 2026-07
orchestrator/.venv/bin/python factory/flows/freight-reconciliation/tests/live/compare_runs.py RUN_DIR_A RUN_DIR_B
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
  The flow digest recorded at init (flow files plus the flow.yml's `digest_include` globs) must still
  match, or `advance` returns `flow_changed`.
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
`definitions/` (the rate spec and every node output schema), `prompts/`, `gates/`, `scripts/`, `tests/`. CLI
for script nodes and gates: `python -m freight --help` (JSON on stdout; errors exit 1 with at most 10
readable problem lines on stderr, which is what a failed gate's feedback shows the worker).

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
  discounts gated on consignments in the billing month, term, gaps, `non_pricing` clauses and
  `unrepresentable` terms (any entry stops the run). Conditions are three-valued: `{"unrecorded": ...}`
  names a fact the shipment records do not hold. Bands keep the contract's inclusive/exclusive ends;
  values in no band are never snapped.
- **Pricing** (`pricing.py`): prices from `shipments.json` + spec only; flags: `NO_SHIPMENT_MATCH`,
  `SHIPMENT_AMBIGUOUS`, `SHIPMENT_DATA_MISSING`, `CARRIER_MISMATCH`, `OUTSIDE_TERM`, `CONTRACT_GAP`,
  `OUTSIDE_RATE_CARD`, `UNRECOGNIZED_CHARGE`, `UNCONTRACTED_CHARGE`, `CHARGE_NOT_APPLICABLE`,
  `CHARGE_UNVERIFIABLE` / `CONDITION_UNVERIFIABLE` (only an unrecorded fact could settle it; escalated),
  `DUPLICATE_BILLING` (earliest billing across all periods wins; later ones expect 0),
  `SERVICE_NOT_OFFERED`, `NOT_DELIVERED`, `COMPONENT_ARITHMETIC`, `ATTRIBUTE_MISMATCH`, `UNDERBILLED`,
  credit-note flags. Credit line expected = original expected − original billed − earlier credits;
  the original is flagged `CREDIT_NOTE_OFFSET`. Invoice findings: `ADJUSTMENT_MISMATCH`,
  `ADJUSTMENT_UNDETERMINED`, `INVOICE_TOTAL_MISMATCH`.
- **Policy** (`policy.py`): effects escalate > offset > dispute > base (dispute if billed − expected >
  tolerance, else accept); a `judgement` flag turns the result into `[outcome, escalate]` for
  adjudication. Unknown flags or missing policy entries fail closed.
- **Report** (`report.py`): dispositions only from policy or from adjudications restricted to the
  offered options (an adjudication's clause ids are appended to its justification; `contract_clause`
  stays the clauses the expected amount comes from); deterministic justifications with clause citations; `verify()` enforces
  `report.schema.json`, one row per in-scope line, delta = billed − expected, recomputed totals,
  no disputed line offset by a credit note (each rupee counted once), and policy-allowed dispositions.
- **Tests**: real data only for structure and the scope rule; every pricing expectation uses the
  synthetic `acme` carrier in `tests/synthetic.py`.

## Freight agent nodes (Phase 6)

Three agent nodes, each surrounded by deterministic script nodes and gated per branch. Gates and scripts are
thin bash wrappers around `python -m freight` with `PYTHONPATH=$FLOWSTATE_VAR__flow_dir`.

- **Contract extraction** (`prompts/extract-rules.md` including `prompts/reference/rate-spec-guide.md` and
  the schema; Opus). `rules-plan` builds clause indexes, the shipment vocabulary and each carrier's
  `invoice_charge_codes` (what its parsers declare in `CHARGE_CODES`), looks up the cache, and assigns two
  independent copies (`a`, `b`) per carrier without a valid cached spec. Each copy's branch is gated by
  `gates/rate-spec-traced.sh` (`tracing.py`: identity, clauses exist, every priced number appears in a cited
  clause, every clause accounted for, vocabulary) and `gates/worker-inputs-only.sh` (`audit.py`: the worker's
  transcript shows it read only its assigned inputs and wrote only inside its branch). `rules-agree` compares
  the copies by behaviour (`agreement.py`: probe shipments at and between every threshold, per charge code the
  carrier bills): agreed → adopted; disagreed → one fresh round (`c`, `d`) that must agree copy against copy
  (`rules-final`); invalid, unrepresentable or still disagreeing → the script exits 1 and the run stops for a
  human. Cache entry: `<cache_dir>/<carrier>/<sha256(contract sha | format version | prompt version)>.json`,
  re-traced on every use and reused only for the same shipment vocabulary and invoice codes.
- **Adjudication** (`prompts/adjudicate.md`; Sonnet). `adjudication-plan` batches the items policy left open
  into packets (computed facts, the two options, full clause text). `gates/adjudications-grounded.sh`
  (`adjudication.check`): each item decided once, an offered disposition, real clause ids, a justification
  of at most 600 characters whose figures all come from the packet (`grounding.py`). `merge-adjudications`
  re-checks every batch and requires every open item decided exactly once. Decisions have no amount fields.
- **Memos** (`prompts/write-memos.md` including `prompts/reference/memo-style.md`; Sonnet). `memo-plan` makes
  one facts entry per non-accept report row; `gates/memos-grounded.sh` (`memos.check`): coverage, field
  lengths, grounded figures; `render-memos` writes `memos/<memo_id>.md` with a facts table generated from the
  report, and refuses a directory holding anything else.
- **Isolation flows** (`tests/stages/stage-{rules,adjudicate,memos}.{dot,flow.yml}`): `tests/stage_flows.py`
  copies the flow's assets next to a stage graph, so the real prompts, schemas, gates and scripts run through
  flowstate. `tests/test_phase6_stage_runs.py` drives them with fake workers (gates pass; gate failure →
  retry; disagreement → second round; cache reuse); `tests/live/run_stage.py` runs them with real workers and
  a minimal scripted supervisor (not the graph-orchestrator skill).

## Reconciliation flow and reconcile-freight skill (Phase 7)

- **Entry points**: `/reconcile-freight 2026-07` in Claude Code (`.claude/skills/reconcile-freight/SKILL.md`:
  validate → init with only the inputs the human gave → supervise with graph-orchestrator, using a table of
  what this flow's situations mean → report from `publication.json`), or headless
  `factory/flows/freight-reconciliation/reconcile.sh 2026-07`: a `claude -p` orchestrator with Bash limited to
  `orchestrator/bin/flowstate`, Read/Grep/Glob and the Skill tool (env `MODEL`, `BUDGET`, `RUNS_DIR`, `RUN_ID`,
  `PUBLISH_DIR`, `FRESH_EXTRACTION`); the orchestrator's transcript and tool calls go to
  `<RUNS_DIR>/<RUN_ID>.orchestrator/`.
- **Graph** (`freight-reconciliation.dot`): `discover` [scope-resolved] → `rules_plan` → extraction region →
  `rules_agree` → second-round region → `rules_final` → `price` [priced-covers-scope] → `adjudication_plan` →
  adjudication region → `merge_adjudications` → `assemble` → `memo_plan` → memo region → `render_memos` →
  `publish`. Parsing is one script node rather than a per-file fan-out: every branch rewrites `state.yaml`,
  and parsing is cheap sequential code.
- **Inputs**: only `period` is required. Data and config paths, `rules_cache_dir` (`runs/_cache/rate-specs`),
  `use_rules_cache`, batch sizes and `publish_dir` (repository root) have defaults; paths may be
  repository-relative, resolved by `scripts/_paths.sh` because script nodes run in the artefact directory.
- **Artefacts** (`runs/<run_id>/artefacts/`): `discovery/` (manifest, parsed documents), `rules/` (plan, clause
  indexes, vocabulary, `agreement-1.json`, `final.json`, adopted `specs/`), `branches/<id>/` (each worker's
  output), `pricing/priced.json`, `adjudication/` (packets, batches, merged decisions),
  `report/reconciliation-report.json`, `memo-work/`, `memos/`, `publication.json`.
- **Publish** (`freight/publish.py`): re-verifies the report (schema, invariants, one row per in-scope line) and
  that the run's memos are exactly one per non-accept row, then copies `reconciliation-report.json` and
  `memos/` to `publish_dir`; an existing `memos/` is replaced only if it holds nothing but `.md` files.
- **Stable output**: report text, finding ids and memo ids never use names a rate spec chose (component or
  adjustment names differ between agreeing extractions). Adjustments are identified by what they do
  (`pricing.adjustment_terms`, e.g. `discount-5pct-from-13`); flags name the charge's kind.
- **Digest**: the flow.yml's `digest_include` (`freight/**/*.py`, `config/*.yml`, `scripts/_paths.sh`) makes a
  change to pricing code, policy or carrier config during a run a `flow_changed` situation. Do not edit
  those files while a run is active.
- **Tests**: `tests/test_phase7_flow.py` runs the real flow end to end with fake workers on synthetic acme
  data (every gate, publication); `test_phase7_wiring.py` (coverage checks, publish refusals, the spec map,
  the flow's schema copy equals `report.schema.json`); `test_phase7_skill.py` (the skill names only real
  commands, flags, nodes, gates, variables and artefact paths); `orchestrator/tests/test_flowstate_digest.py`.
  `tests/live/compare_runs.py` compares two real runs (specs, amounts, and which dispositions differ and
  whether policy or adjudication decided them).

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
  `integer`, `boolean`, `dict`, `list`, `any`; optional `default`, `required`). `item` is reserved. Optional `digest_include`: globs, inside the flow directory, of files the
  scripts use indirectly (an imported package, config); they join the flow digest.
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
