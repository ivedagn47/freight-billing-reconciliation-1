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

This checkout is a **partial scaffold**, not a working system:
- `orchestrator/lib/flowstate/` and `orchestrator/lib/agentctl/` (and its `harnesses/` subdir) are
  **empty directories** — the actual CLI implementations referenced by `orchestrator/bin/flowstate`
  and `orchestrator/bin/agentctl` do not exist yet in this copy.
- `.claude/skills/graph-orchestrator/SKILL.md`, referenced by `PROBLEM.md` as the orchestrator
  protocol skill, does not exist in this checkout either.
- Only the two demo flows (`factory/flows/smoke-test/`, `factory/flows/smoke-branch/`) and the
  `factory-prefs-example.yml` default are present under `factory/`.

Before assuming any orchestrator command works, verify the relevant Python module actually exists
under `orchestrator/lib/`. If it's still missing, that implementation is itself part of the task
(`PROBLEM.md` explicitly permits modifying "the orchestrator skill, and the flowstate/agentctl code
itself, if your design calls for it" — noting what changed and why in `DESIGN.md`), or the graph
approach may not be the right call at all — a skills-only or fully-steered design (see `brief.md`
§6) is equally valid if simpler and the reasoning is documented.

## Setup and commands

```bash
orchestrator/setup.sh        # one-time: creates orchestrator/.venv, installs pyyaml/jsonschema/pydot
                              # requires python3, git, tmux, jq on PATH
orchestrator/bin/flowstate   # flow CLI  — PYTHONPATH-wrapped invocation of `python -m flowstate`
orchestrator/bin/agentctl    # worker lifecycle CLI (spawn/wait/send/kill) — `python -m agentctl`
```

There is no build, lint, or test tooling defined anywhere in this repo yet — none of `package.json`,
a Python project file, a Makefile, or a CI config exist. If you add flowstate/agentctl code or
scripts, decide on and document (in `DESIGN.md`) whatever validation you introduce for it.

To understand the graph machinery before designing against it, run the two demo flows:
- `factory/flows/smoke-test/` — linear agent-node flow (`research` → `summarise`) with a
  schema-validated output at each step and a shell **gate** (`gates/brief-exists.sh`) on the edge
  between them, reading `FLOWSTATE_VAR_research_brief`.
- `factory/flows/smoke-branch/` — `fork` → parallel `worker_a`/`worker_b` (script-runner nodes,
  not agent nodes) → `join` (with a reducer script maintaining a summary variable) → `merge_notes`.

## Flow file architecture (DOT + flow.yml)

A flow is two files per directory, `factory/flows/<name>/<name>.dot` and
`factory/flows/<name>/<name>.flow.yml`, plus supporting `prompts/`, `scripts/`, `gates/`,
`definitions/` subdirs:

- **`.dot`** (Graphviz digraph) declares nodes and edges. Node attributes select a runner and its
  config: agent nodes (`shape=box` with `prompt_template`, `output_schema`, `model`,
  `permission_mode`, `pause_at`, `working_dir`) get a fresh worker session per node with a rendered
  prompt; `runner=script` nodes run a deterministic shell script instead of an LLM; `runner=fork`,
  `runner=join` (with `reducer_script`, `summary_var`) handle parallel fan-out/fan-in. Edges may
  carry `gates` (shell scripts that must `exit 0` to proceed) and conditions.
- **`.flow.yml`** declares `output_schemas` (mapping a node's `output_schema` name to the files it
  must produce, each validated against a JSON Schema in `definitions/`, and which flow `variables`
  those files populate) and the typed `variables` themselves (`string`, `path`, `dict`, ...).
- Variables populated by one node are exposed to scripts/gates downstream as
  `FLOWSTATE_VAR_<name>` environment variables (see `gates/brief-exists.sh`,
  `scripts/worker_a.sh`).
- `working_dir="{_run_artefact_dir}"` — the per-run artefact directory is a built-in templated
  variable; node outputs generally land there.

The engine (flowstate/agentctl, once implemented) owns run state, schema validation, and worker
lifecycle; it does *not* itself decide how to respond to a failed gate, stalled worker, or
ambiguous branch — that judgment is delegated to an **orchestrator agent** running on top of the
CLIs (per `brief.md` §5), which is the role the (currently missing) graph-orchestrator skill is
meant to fill.

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
