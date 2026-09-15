# Design: freight billing reconciliation

## How to run it

```bash
orchestrator/setup.sh                                          # once: venv with pyyaml, jsonschema, pydot
# interactive, in Claude Code at the repository root:
/reconcile-freight 2026-07
# or headless (a Claude orchestrator session limited to flowstate commands):
factory/flows/freight-reconciliation/reconcile.sh 2026-07      # FRESH_EXTRACTION=1 to re-read every contract
```

Both publish `reconciliation-report.json` and `memos/` at the repository root. The submitted files come from run
`freight-2026-07` (see *The submitted run* below); its evidence is committed under `runs/freight-2026-07/` and
`runs/freight-2026-07.orchestrator/`.

Tests spend no tokens:

```bash
(cd orchestrator && .venv/bin/python -m pytest)                                          # runtime, skill: 159
(cd factory/flows/freight-reconciliation && ../../../orchestrator/.venv/bin/python -m pytest)  # reconciliation: 207
```

`IMPLEMENTATION_LOG.md` is the phase-by-phase record behind these notes (decisions, experiments, failures and
fixes, costs). `CLAUDE.md` is the map of the code.

## The submitted run

- **How it ran.** Run `freight-2026-07` was started headless:
  `FRESH_EXTRACTION=1 RUN_ID=freight-2026-07 factory/flows/freight-reconciliation/reconcile.sh 2026-07`.
  A Sonnet orchestrator loaded `reconcile-freight` and `graph-orchestrator` and validated the flow. It
  initialised the run with only `period=2026-07` and `use_rules_cache=false`, then advanced once to completion,
  in 2 min 22 s (04:08:46–04:11:08 UTC).
- **Work.**
  - 8 workers ran (6 Opus extraction copies, 1 adjudication batch, 1 memo batch), and all exited successfully.
  - 18 gates passed, with no retries, respawns or pauses.
  - All three contracts were read afresh, and for each the two copies agreed in the first round (144 / 2,304 /
    144 probe shipments, no differences). Nothing came from the cache.
- **Result.**
  - 127 invoice lines: 122 accept, 4 dispute, 1 escalate; plus one escalated invoice finding. ₹8,832 is in
    dispute.
  - `total_expected` is null because one Alpine consignment weighs exactly 50 kg, which neither of the
    contract's rate bands ("under 50 kg", "over 50 kg") covers. That line has no contract amount, so the
    invoice's volume discount cannot be quantified either.
  - 6 memos.
  - The published files are byte-identical to the run's artefacts, and the report's SHA-256 is recorded in
    `artefacts/publication.json`.
- **Consistency.** Two earlier dry runs extracted every contract independently. Against those, the adopted
  specs price identically, and every amount, finding, total, disposition and memo file matches; only the
  wording of agent prose differs.
- **Cost.** $1.75 for the workers and $0.26 for the orchestrator.
- **Orchestrator behaviour.** It made no interventions and reported from `publication.json`. Once it tried
  to read the published report with a `python3 -c` shell command. The launcher's permission mode denied it
  (the skills require the Read tool), and it continued with Read and Grep. So the rule was enforced by
  permissions, not by the model.
- **Where to look.**
  - `runs/freight-2026-07/state.yaml`: every node, attempt and gate evaluation.
  - `events.jsonl`: the ordered history.
  - `artefacts/`:
    - the discovery manifest;
    - every extraction copy under `branches/`;
    - the agreement record;
    - priced lines;
    - adjudication packets and decisions;
    - the report, memo drafts and memos.
  - `workers/*/transcript.jsonl`: each worker's full session.
  - `logs/`: script and gate output.
  - `runs/freight-2026-07.orchestrator/`: the orchestrator's prompt, transcript, tool calls, result (including
    the denial), final report and launcher log.

## 1. Architecture and why

**A Flowstate graph, supervised by an agent that follows two skills.** The work is recurring, multi-step and
moves money. A skipped or unvalidated step is expensive, and a run has to hold up every time, not once. That is
the case `brief.md` §6 describes for a graph. The alternatives lose on exactly that:
- **A freewheeling agent** decays over a long context and varies from run to run.
- **A skill chain** lets steps be skipped as instructions fade.
- **A framework graph** handles only what its author coded and would have discarded the kit.

The kit's Flowstate, agentctl and orchestrator skill were empty scaffolds, so they were built first (§4).

**Division of labour.**

| Part | Does | Never does |
|---|---|---|
| Deterministic code (`freight/`, script nodes) | Parse invoices, decide scope, price lines from shipment records and rate specs, apply the disposition policy, assemble, verify, publish | Read prose contracts or write prose |
| Agent workers (Flowstate nodes) | Read a contract into a rate spec (Opus); choose between two policy-offered dispositions (Sonnet); write memo prose (Sonnet) | Compute or change an amount, choose outside the offered options, read anything beyond their assignment |
| Gates and merge/publish checks | Prove each output is well formed, traced to its sources and complete | Repair outputs |
| Flowstate + agentctl | Order, state, schema validation, gates, isolated worker lifecycle, retries, recovery | Judge what a failure means |
| Orchestrator agent (`graph-orchestrator` + `reconcile-freight` skills) | Read each situation and its evidence; retry, respawn or pause | Edit anything, run workers, or influence a disposition, amount, clause reading or memo text |

**The graph** (`factory/flows/freight-reconciliation/freight-reconciliation.dot`), with gates in brackets:

```
discover [scope resolved]
 -> rules_plan -> extract_rules x2 per carrier [traced to the contract, worker audited] -> rules_agree
 -> (second round only if the copies disagreed) -> rules_final
 -> price [every in-scope line priced exactly once]
 -> adjudication_plan -> adjudicate per batch [grounded, audited] -> merge_adjudications
 -> assemble (schema + invariants)
 -> memo_plan -> write_memos per batch [grounded, audited] -> render_memos
 -> publish (everything re-verified) -> done
```

**Why the agents sit only there.**
- **Contracts are prose.** A reading is transcribed into a closed rate-spec format (quantities, flat / per-unit /
  banded / percentage components, shipment conditions, invoice discounts, term, gaps) that code evaluates.
  Nothing numeric is ever calculated by a model.
- **Some lines need judgement that policy deliberately does not encode**, for example an undelivered shipment
  or an invoice whose own charges do not add up. For those, policy offers exactly two dispositions and an agent
  picks one with a reason.
- **Memos are written for a person**, so an agent drafts the prose around a facts table that code renders
  from the report.

**Scale.** Parsing, pricing, policy and checks are linear sequential code. Agent work grows with the number of
contracts, which are cached, and with the number of ambiguous lines, which are batched. It does not grow with
the number of lines. Every agent step is a dynamic fan-out with parallel branches, and batch sizes are run
inputs. Adding a carrier takes one config entry, plus a parser only if its invoice format is new. A new month is
`--var period=`.

## 2. What is validated, where, and why there

The rule: check at the earliest point where the evidence exists, check agent output inside its own branch (so a
retry reaches the worker that can fix it), and re-check at every merge and before publishing (so nothing
downstream trusts upstream).

| Where | Check | Why there |
|---|---|---|
| Every worker (agentctl) | Only file tools; no shell, web, project CLAUDE.md, skills, memory or MCP. Verified by probe, not assumed | A worker must see only what the graph gives it |
| Every node output (Flowstate) | JSON Schema; `_session_id` equals the session Flowstate assigned | Structure, and proof the file came from that worker |
| `discover` → gate `scope-resolved` | No document whose period or target invoice is unknown; something in scope | An unresolved document is a human question, not a guess |
| Parsers | Strict formats; the invoice's own arithmetic recorded as integrity facts, never corrected | Carrier errors must surface, not disappear |
| Each extraction copy → gates `rate-spec-traced`, `worker-inputs-only` | Every priced number appears in a clause it cites; every clause is accounted for; term and agreement match the header; conditions use values that exist in the shipment records. The worker's transcript shows it read only its contract and clause index | A spec that cannot be traced is not a transcription. An extraction that looked elsewhere is not independent |
| `rules_agree` / `rules_final` | Two independent copies must price identically on probe shipments at and between every threshold, for every charge code the carrier bills. One fresh round if they disagree; otherwise the run stops for a human. Terms the format cannot express stop the run | A single reading of prose is a lucky guess until a second independent reading agrees |
| Rate-spec cache | Key = contract hash + format version + prompt version. Re-traced on every use and reused only for the same shipment vocabulary and invoice codes | Re-reading an unchanged contract every run is waste; reusing a stale reading is worse |
| `price` → gate `priced-covers-scope` | Every in-scope line priced exactly once; every line and finding decided or offered for judgement | At volume the danger is a silently dropped or duplicated line |
| Each adjudication batch → gates `adjudications-grounded`, `worker-inputs-only`; then `merge_adjudications` | Each open line decided once, with an offered disposition, real clause ids, and every quoted figure present in the packet; all batches re-checked and every open line covered once | The agent may choose; it may not invent |
| `assemble` | `report.schema.json`; one row per in-scope line; delta = billed − expected; totals recomputed; a line offset by a credit note is never disputed (each rupee counted once); dispositions allowed by policy | The fixed contract and the double-counting rule |
| Each memo batch → gates `memos-grounded`, `worker-inputs-only`; then `render_memos` | Every non-accept row gets exactly one memo; field lengths; quoted figures present in that memo's facts; the facts table is rendered by code | A memo must not state a number the report does not |
| `publish` | Report and memos re-verified before copying to the repository; unrelated files never deleted | The last line of defence before anyone acts on the output |
| Flow digest (with `digest_include`) | Flow files, the `freight` package and config unchanged since the run started | A code or policy change mid-run would make the output unreproducible |
| Orchestrator (skills + launcher permissions) | Bash limited to `orchestrator/bin/flowstate`; no Write or Edit; retry / respawn / pause only through Flowstate, within the node's budget | Supervision must not become a back door around the checks |

## 3. Judgement calls in the reconciliation

**Confirmed with the user before implementation.**
1. **Credit notes.** A credit-note line appears under its own id. Its expected amount is the correction the
   contract entitles BlueFin to (original expected − original billed, less earlier credits), and its delta is
   the credit issued minus that. The original line is accepted with an offset note, so the residual is counted
   once, on the credit line.
2. **Scope.** The period's invoices plus credit notes correcting them. `SAGAR-CN-01` corrects an August invoice
   and is out of scope. Other months are read only to detect duplicate billing.
3. **Underbilled lines** are accepted with a note; the expected amount stays what the contract says.
4. **Extraction.** Two independent Opus extractions plus an agreement and tracing check, cached.

**Made during implementation** (all explicit in `config/policy.yml` or the code, and recorded in the log).
- **Price basis.** Prices come only from BlueFin's shipment records and the contract. Weight, distance or
  service stated on the invoice is compared as evidence, never used to price.
- **Rate bands keep the contract's wording.** "Under 50 kg" and "over 50 kg" leave exactly 50 kg unrated. Such
  a line is never snapped to a band: its amount is undetermined and it is escalated.
- **Duplicate billing.** The earliest billing of a consignment (across all months) is payable. Every later one
  expects 0 and is disputed, citing no contract clause.
- **Charges the contract does not provide for.**
  - A recognised charge the contract does not provide for (e.g. detention) is disputed.
  - An unrecognisable charge label is escalated.
  - A contracted charge the shipment does not qualify for is disputed through the difference.
- **Facts the records do not hold.** A contract may condition a charge on a fact BlueFin's records don't hold
  (e.g. a delivery address being residential when the booking is not flagged). The charge is excluded from the
  contract amount, and if billed, the line is escalated rather than disputed.
- **A generic "handling fee"** is checked against the contract's handling-type charges (e.g. protected handling
  for fragile goods): payable where the shipment qualifies, rather than disputed outright.
- **Left to bounded adjudication:** undelivered shipments, a service level the contract does not offer, and
  lines whose own charges do not add up on the invoice. Policy offers `[its outcome, escalate]`; nothing else
  is possible.
- **Volume discounts:**
  - computed on the contract-correct total of the invoice's lines;
  - consignments counted from shipment records by carrier and ship month, including shipments not yet
    delivered;
  - if any line on the invoice is undetermined, the discount is unquantifiable and escalated. This also makes
    that invoice's and the summary's expected total null, as the schema requires.
- **Invoice total mismatch** is disputed when it overcharges and accepted otherwise. The tolerance is ₹0.01.
- **Report fields.**
  - `contract_clause` lists the clauses the expected amount is computed from; an adjudicator's cited clauses are
    appended to its justification.
  - `counts_by_disposition` counts lines.
  - `total_billed` includes credit notes.
  - `total_in_dispute` sums disputed line deltas and disputed finding impacts, never both for the same rupee.

## 4. What was changed in the kit, and why

- **agentctl (`orchestrator/lib/agentctl/`)** was empty and was implemented:
  - workers run as `claude -p --output-format stream-json` in detached tmux sessions, recorded in a registry
    inside the run;
  - states include stall detection; `send` resumes the same session; `kill` works on process groups;
  - a deterministic `fake` harness allows token-free tests.
  - Worker isolation flags were chosen by experiment: `--setting-sources ""` alone still exposed bundled skills
    and auto-memory, so `--disable-slash-commands` and `--safe-mode` were added.
- **Flowstate (`orchestrator/lib/flowstate/`)** was empty and was implemented:
  - loading with static validation, including dataflow of every variable;
  - authoritative `state.yaml` with locking, and append-only events;
  - agent and script nodes, JSON Schema validation with session-id proof, gates, safe conditions (no `eval`);
  - `fork`, `join` with reducers, and `dynamic_fanout`;
  - situations, retry, respawn, pause, resume, abort;
  - recovery after `advance` is killed.

  Two design choices go beyond the kit's hints:
  - script nodes run detached so branches truly run in parallel;
  - `flow.yml` `digest_include` adds files a flow uses indirectly to the run's digest.
- **`.claude/skills/graph-orchestrator/`** (referenced by `PROBLEM.md` but absent) was written as a
  situation-driven supervision procedure. `situations.md` is tested against the runtime so it cannot drift.
  `.claude/skills/reconcile-freight/` was added as the entry point.
- **Smaller changes:**
  - kit shell files lacked the executable bit (the `bin/` wrappers were fixed; scripts run through their `#!`);
  - `smoke-branch/scripts/reduce.sh` was missing and was added;
  - `factory/flows/smoke-fanout/` was added because the kit had no fan-out demo;
  - `pause_at=optional` is interpreted as pausing only under `supervision: high`, since the kit defines it nowhere.

## 5. Post-implementation notes

**Live runs found things no test had.**
- A Falcon clause charges residential delivery when the booking is flagged *or* the address is residential. One
  Opus copy stopped the run as unrepresentable, which was correct; the other silently dropped half the
  condition. That led to the `unrecorded` condition and to comparing copies on the charge codes a carrier
  actually bills.
- Published finding ids, memo file names and messages embedded names the extraction worker chose. Identical money
  could have produced different outputs on two runs, so they are now derived from what a term does
  (`discount-5pct-from-13`).
- A duplicate billing cited clauses that do not produce its ₹0, and a memo writer then made a claim the contract
  does not support.
- A relative cache path silently missed the cache, because script nodes run inside the run directory.
- Agent prose quoted CSV column names.

**Run-to-run behaviour.** Three July dry runs, two of them concurrent with independent re-extraction of every
contract, and then the submitted run all published a validated report:
- the two independent runs adopted rate specs that price identically on every probe;
- every amount, finding, total and disposition matched;
- the one adjudicated line was decided the same way;
- the same memo files were published with identical facts tables; only the wording of memo prose varied.

**Worker isolation had to be proven, not assumed.** The probe (`orchestrator/tests/live/probe_isolation.py`)
caught leaks through bundled skills and a shared auto-memory directory that the obvious flag alone did not
close. The audit gate turned "independent extractions" from a prompt instruction into something checked from
transcripts.

**Operational notes.**
- A July run takes about 2.5–3 minutes and costs about $2: roughly $1.75 for six Opus extractions, one
  adjudication batch and one memo batch, plus about $0.25 for the orchestrator. With the cache warm it is a
  fraction of that.
- Twice the account's session limit ended the orchestrator session after its run had completed. The run's own
  state was unaffected, and the launcher now says so plainly.
- One pre-existing Flowstate test asserts wall time and failed once under load.

**What I would do next.**
- **Human approval of a contract reading.** Today a disagreement or an unrepresentable term stops the run with
  the evidence on disk, but nothing lets a person approve a spec into the cache.
- **A persistent ledger of billed consignments**, instead of re-parsing every month's documents to detect
  duplicates.
- **Measure Flowstate with hundreds of branches.** `state.yaml` is rewritten on every change, so branch count
  is the scaling risk.
- **Quantify a partial volume discount** when only some lines are undetermined.
- **Load tests on larger synthetic volumes.** Performance has not been measured beyond this month's 127 lines.
