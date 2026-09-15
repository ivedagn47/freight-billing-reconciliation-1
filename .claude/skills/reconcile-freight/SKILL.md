---
name: reconcile-freight
description: Reconcile one billing period of carrier freight invoices against BlueFin's shipment records and rate contracts by starting the freight-reconciliation flowstate graph, supervising it with the graph-orchestrator skill, and reporting where reconciliation-report.json and memos/ were published. Use when asked to reconcile freight invoices or run the freight reconciliation for a month, e.g. /reconcile-freight 2026-07.
---

# Reconcile freight invoices

You start and supervise one run of the `freight-reconciliation` flow. The flow does all of the work:
code parses the invoices, decides the scope, prices every line from shipment records and the contracts,
applies the disposition policy and verifies everything; agent workers that Flowstate launches extract
the contracts, adjudicate the lines policy leaves open, and draft memos. You do none of that work yourself,
and you never decide or influence an amount, a disposition or a reading of a contract.

## Inputs

- **Billing period** `YYYY-MM` (required), from the request: `/reconcile-freight 2026-07` means `2026-07`.
  If it is missing or malformed, ask for it; never guess.
- Only if the human asks for them: a publish directory (default: the repository root), a fresh extraction of
  every contract (`use_rules_cache=false`), a run id, a runs directory. Data and config paths default to the
  repository's files; do not set them.

## Procedure

1. Load the **graph-orchestrator** skill with the Skill tool. Its command rules, its Never list and its
   decision procedure apply for the whole run; this skill only adds what this flow's situations mean.
2. `orchestrator/bin/flowstate validate freight-reconciliation`. If `ok` is false, report the issues and stop.
3. Start the run, with only the inputs you were given:
   `orchestrator/bin/flowstate init freight-reconciliation --var period=2026-07`
   (add `--var publish_dir=PATH`, `--var use_rules_cache=false`, `--run-id ID`, `--runs-dir DIR` only when
   asked; pass `--runs-dir` to every later command if you used it).
4. Supervise the run with graph-orchestrator's loop (`flowstate advance RUN --max-wait 300`, read the
   situation and its evidence, act) until it completes or you pause it. Use the notes below to understand
   this flow's situations; they refine the decision procedure and never override it.
5. Write the final report.

## What this flow's situations mean

| Where | Situation | Meaning | Action |
|---|---|---|---|
| `discover` | `gate_failed` (`gates/scope-resolved.sh`) | A document's billing period or target invoice could not be established, or nothing is in scope | Pause: the documents, the carrier config or the period need a human |
| `extract_rules`, `extract_rules_rerun` | `gate_failed` (`gates/rate-spec-traced.sh`) | The rate spec names a number that is not in the clause it cites, misses a clause, or uses a value the shipment records do not have | Retry that branch, quoting the gate's stderr |
| any agent node | `gate_failed` (`gates/worker-inputs-only.sh`) | The worker read or wrote files outside its assignment; its transcript cannot be repaired | Respawn that branch with the gate's stderr as the reason; if it happens again, pause |
| `rules_agree`, `rules_final` | `script_failed` | A contract could not be adopted: a copy was invalid, the contract has a term the format cannot express, or two rounds of independent copies disagreed | Pause, and point the human at `artefacts/rules/agreement-1.json` or `artefacts/rules/final.json` in the run directory. Never try to obtain agreement by retrying |
| `price` | `gate_failed` (`gates/priced-covers-scope.sh`) | A line in scope was not priced exactly once, or not decided | Pause |
| `adjudicate` | `gate_failed` (`gates/adjudications-grounded.sh`) | A decision is missing or duplicated, uses a disposition that was not offered, cites a clause that does not exist, or quotes a figure that is not in its packet | Retry that branch, quoting the gate's stderr. Never suggest a disposition |
| `write_memos` | `gate_failed` (`gates/memos-grounded.sh`) | A memo is missing, too long, or quotes a figure that is not in its facts | Retry that branch, quoting the gate's stderr. Never supply memo text or figures |
| `discover`, `rules_plan`, `price`, `adjudication_plan`, `merge_adjudications`, `assemble`, `memo_plan`, `render_memos`, `publish` | `script_failed` | Deterministic code refused its inputs (an unparseable invoice, a missing spec, a report or memo that fails verification) | Pause and quote the stderr: the same code on the same inputs fails the same way |
| any | `flow_changed` | The flow, its code, policy or carrier config changed during the run | Pause |

## Never (in addition to graph-orchestrator's list)

- Suggest or decide a disposition, an amount, a rate, a reading of a clause or the wording of a memo, in
  feedback or anywhere else.
- Edit anything under `factory/flows/freight-reconciliation/` (prompts, policy, carrier config, code), under
  `data/`, in the rate-spec cache, or in the published files.
- Write `reconciliation-report.json` or `memos/` yourself, or copy them out of the run directory.
- Set `use_rules_cache`, `publish_dir` or any data path the human did not ask for.

## Final report

When the run completes, read the file named by the `publication` variable in the completed situation, and
report, copying figures from that file rather than calculating anything:

```
COMPLETED run <run_id>
Published: <report path> and <memos dir> (<number of memos> memos)
Summary: total billed <total_billed>, total expected <total_expected>, in dispute <total_in_dispute>;
         accept <n>, dispute <n>, escalate <n> (lines)
Interventions: <each situation -> action -> outcome, or "none">
```

When you paused the run, use graph-orchestrator's PAUSED report.
