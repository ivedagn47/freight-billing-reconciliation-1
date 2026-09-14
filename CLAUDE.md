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
- `orchestrator/lib/flowstate/` — **core runtime implemented (Phase 2)**, see "flowstate" below.
  Acyclic graphs with start/done, agent and script nodes, gates and conditions. `runner=fork`,
  `join` and `dynamic_fanout` are parsed but rejected until Phase 3.
- `.claude/skills/graph-orchestrator/SKILL.md`, referenced by `PROBLEM.md`, does not exist yet
  (Phase 4).
- Kit shell files were committed without the executable bit. Both `bin/` wrappers are now fixed;
  flowstate runs flow scripts and gates through their `#!` interpreter, so the demo-flow scripts
  work unchanged. `smoke-branch.dot` references a `scripts/reduce.sh` that does not exist.
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
(cd orchestrator && .venv/bin/python -m pytest tests/test_lifecycle.py -k stall)  # single test
orchestrator/tests/live/smoke_claude_worker.sh        # real Claude worker via tmux (spends cents)
orchestrator/.venv/bin/python orchestrator/tests/live/probe_isolation.py  # re-verify worker isolation
```

Lifecycle and agent-node tests need a working `tmux`. Scratch runs go to `runs/_*/` (gitignored).

Run the demo flow (fake harness = deterministic scripted workers, no tokens; drop the `--harness`
and `--fake-script` flags to use real Claude workers):

```bash
F=orchestrator/tests/fixtures/fake/smoke-test
orchestrator/bin/flowstate --runs-dir runs/_scratch init smoke-test --var research_topic="tides" \
  --harness fake --fake-script research=$F/research.json --fake-script summarise=$F/summarise.json
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
  (captures from the prompt, writes files, sleeps/heartbeats, fixed exit codes) for token-free tests.
- Worker isolation is enforced in `harnesses/claude.py` and was verified by experiment, not assumed:
  tools limited to Read/Write/Edit/Glob/Grep (`--tools` + `--allowedTools`, Bash/web disallowed),
  `--setting-sources ""` (no project CLAUDE.md/skills), `--disable-slash-commands` (bundled skills),
  `--safe-mode` (auto-memory), `--strict-mcp-config`, `--permission-prompts none`. Dropping any of
  these re-opens a leak that `probe_isolation.py` detects.

## flowstate

`flowstate [--runs-dir DIR] {validate,init,advance,retry,respawn,pause,resume,abort,status,events}`;
JSON on stdout. Situations (including failures) exit 0; command errors exit 1.

- **Loading** (`loader.py`, `validate.py`): pydot parses the DOT (a `graph [...]` statement shows up
  as a pseudo-node named `graph`; values keep their quotes). Node kind comes from the existing
  syntax: `Mdiamond` start, `Msquare` done, `prompt_template` agent, `runner=script` script. All
  issues are collected before any run exists: attribute whitelists per kind, referenced files,
  schema validity, one start/done, acyclic, reachability, conditions on every branch, and a
  dataflow pass that every `{var}` / `$FLOWSTATE_VAR_x` is declared *and* set on every path.
- **Run directory** `runs/<run_id>/`: `state.yaml` (authoritative; only mutated in
  `RunStore.transaction()` under `.state.lock`), `events.jsonl` (append-only, written after the
  state change), `artefacts/`, `workers/` (the agentctl registry), `logs/` (script output, gate
  evidence, snapshots of rejected outputs). `advance`/`retry`/`respawn` take a non-blocking
  `.advance.lock` lease; `pause`/`abort`/`status` do not. The flow digest recorded at init must still
  match, or `advance` returns `flow_changed`.
- **Variables**: run inputs via `init --var`, builtins (`_run_id`, `_run_dir`, `_run_artefact_dir`,
  `_flow_dir`, `_node_id`, `_session_id` for agents), and `sets_variables` (a file name binds the
  file's path; `{file, pointer}` binds a JSON-pointer value). A node's own path bindings are
  pre-bound before it runs, so a prompt can say "write to `{research_brief}`"; they are committed
  only after validation. Missing variables are errors, never empty strings.
- **Agent nodes** (`agent_node.py`): prompt rendered + a runtime footer stating the assigned session
  id; spawned through `agentctl.lifecycle` with a fresh UUID. Every JSON output must carry that exact
  `_session_id`. **Script nodes** run via their `#!` interpreter with `FLOWSTATE_VAR_*` env.
- **Advancing a node**: outputs exist → JSON Schema valid → `_session_id` (agents) → bindings →
  conditions pick exactly one edge → that edge's gates exit 0 → one atomic commit (variables, node
  completed, cursor moves). Any failure becomes a persisted situation and nothing is committed.
- **Situations** (`runtime.py`): persisted until acted on — `validation_failed`, `gate_failed`,
  `worker_failed`, `script_failed`, `node_interrupted`, `render_failed`, `condition_error`,
  `no_route`, `ambiguous_route`, `retries_exhausted`; transient — `worker_running` (`--max-wait`),
  `worker_stalled`, `worker_timeout`, `busy`, `flow_changed`; run-level — `paused`, `aborted`,
  `completed`. Each lists its `options`.
- **Decisions**: `retry` sends structured feedback into the same worker conversation (script nodes
  rerun); `respawn` kills the old worker and starts `<node>.respawn-N` with a new session, keeping old
  evidence. Both consume the node's `max_retries` budget (default from prefs). `pause` stops
  advancement without killing workers; `abort` kills them. `pause_at=optional` pauses only when
  supervision is `high`.
- **Recovery**: an agent attempt is recorded (`launch: pending`) before agentctl is called, so a new
  `advance` process reconnects to or finishes launching that worker instead of spawning twice.
  Completed nodes never rerun; an interrupted script is reported, not silently rerun.

The runtime deliberately does not decide how to respond to situations — that judgement belongs to an
**orchestrator agent** driving the CLI (per `brief.md` §5), the role of the graph-orchestrator
skill (Phase 4).

## Flow file architecture (DOT + flow.yml)

A flow is two files per directory, `factory/flows/<name>/<name>.dot` and
`factory/flows/<name>/<name>.flow.yml`, plus supporting `prompts/`, `scripts/`, `gates/`,
`definitions/` subdirs:

- **`.dot`** (Graphviz digraph) declares nodes and edges. Node attributes select a runner and its
  config: agent nodes (`shape=box` with `prompt_template`, `output_schema`, `model`,
  `permission_mode`, `pause_at`, `working_dir`) get a fresh worker session per node with a rendered
  prompt; `runner=script` nodes run a deterministic shell script instead of an LLM; `runner=fork`,
  `runner=join` (with `reducer_script`, `summary_var`) handle parallel fan-out/fan-in. Edges may
  carry `gates` (comma-separated scripts that must `exit 0`) and a `condition`
  (`count > 0 and status == "ok"`; operators `== != < <= > >=`, `and`/`or`, no parentheses).
- **`.flow.yml`** declares `output_schemas` (mapping a node's `output_schema` name to the files it
  must produce, each validated against a JSON Schema in `definitions/`, and which flow `variables`
  those files populate) and the typed `variables` themselves (`string`, `path`, `number`,
  `integer`, `boolean`, `dict`, `list`, `any`; optional `default`, `required`).
- Variables populated by one node are exposed to scripts/gates downstream as
  `FLOWSTATE_VAR_<name>` environment variables (see `gates/brief-exists.sh`,
  `scripts/worker_a.sh`).
- `working_dir="{_run_artefact_dir}"` — the per-run artefact directory is a built-in templated
  variable; node outputs generally land there.

To see the machinery, run the two demo flows:
- `factory/flows/smoke-test/` — linear agent-node flow (`research` → `summarise`) with a
  schema-validated output at each step and a shell **gate** (`gates/brief-exists.sh`) on the edge
  between them, reading `FLOWSTATE_VAR_research_brief`. Runs end to end today.
- `factory/flows/smoke-branch/` — `fork` → parallel `worker_a`/`worker_b` (script-runner nodes,
  not agent nodes) → `join` (with a reducer script maintaining a summary variable) → `merge_notes`.
  Needs Phase 3.

## Reconciliation data

- `data/shipments.json` — BlueFin's own shipment records; ground truth for what was actually
  shipped. Keyed by `shipment_id`, cross-referenced from invoices via `carrier_consignment_ref`.
- `data/contracts/*.md` — one prose rate contract per carrier (alpine-express, falcon-freight,
  sagar-roadlines). **The contracts are the sole authority on what anything should cost** — expected
  amounts and dispute/escalate justifications must trace back to a specific clause
  (`contract_clause` field in the report schema).
- `data/invoices/` — five July 2026 invoices, one per carrier billing cycle, in three different
  native formats: Alpine as JSON (`ALPINE-*.json`), Falcon as free-text tax-invoice layout
  (`FALCON-*.txt`), Sagar as CSV (`SAGAR-*.csv`). Each carrier's format is internally consistent but
  differs from the others — normalizing these into a common line-item shape before matching against
  contracts/shipments is a core part of the design. Credit notes (`FALCON-CN-01.txt`,
  `SAGAR-CN-01.csv`) are also present and need handling.

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
