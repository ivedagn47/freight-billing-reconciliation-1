"""Adjudication: the judgement policy leaves open, prepared and checked by code.

Policy settles most lines. A line with a `judgement` flag is offered exactly two dispositions, and an
agent chooses between them. Code does everything around that choice:

    plan    batch the open items; one packet per batch with each item's computed facts (amounts,
            components, flags, shipment evidence), its options, and the full clause text of every
            contract involved
    check   (gate) every item decided exactly once, with an offered disposition, clause ids that exist
            in that carrier's contract, and a justification whose figures are all copied from the packet
    merge   every batch re-checked, every open item decided exactly once across batches

The agent never outputs an amount: its decision has no amount fields, and report assembly takes every
amount from pricing.
"""

import json
from pathlib import Path

from . import grounding
from .rules import write_json

MAX_JUSTIFICATION = 600
FACT_KEYS = ("item_id", "invoice", "doc_type", "carrier", "line_no", "consignment_ref", "shipment_id", "billed_amount",
             "expected_amount", "delta", "contract_file", "contract_clauses", "components", "quantities",
             "billed_charges", "flags", "evidence")


class AdjudicationError(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def contract_facts(index: dict) -> dict:
    return {"file": index["file"], "agreement_ref": index["agreement_ref"], "term": index["term"],
            "clauses": [{"id": c["id"], "section": c["section"], "text": c["text"]} for c in index["clauses"]]}


def load_contracts(clauses_dir: Path, carriers: set[str]) -> dict:
    out = {}
    for carrier in sorted(carriers):
        path = Path(clauses_dir) / f"{carrier}.json"
        if not path.is_file():
            raise AdjudicationError([f"no clause index for carrier {carrier} at {path}"])
        out[carrier] = contract_facts(json.loads(path.read_text()))
    return out


def plan(bundle: dict, clauses_dir: Path, batch_size: int, out_dir: Path) -> dict:
    if batch_size < 1:
        raise AdjudicationError(["batch size must be at least 1"])
    lines = {line["item_id"]: (n, line) for n, line in enumerate(bundle["priced"]["lines"])}
    needs = sorted(bundle["planned"]["needs_judgement"],
                   key=lambda item: (lines[item["item_id"]][1]["carrier"], lines[item["item_id"]][0]))
    batches = []
    for start in range(0, len(needs), batch_size):
        chunk = needs[start:start + batch_size]
        batch_id = f"adj-{start // batch_size + 1:03d}"
        items = [{**{k: lines[i["item_id"]][1][k] for k in FACT_KEYS}, "options": i["options"],
                  "policy_basis": i["basis"]} for i in chunk]
        packet = {"batch_id": batch_id, "items": items,
                  "contracts": load_contracts(clauses_dir, {item["carrier"] for item in items})}
        path = Path(out_dir).resolve() / "packets" / f"{batch_id}.json"
        write_json(path, packet)
        batches.append({"batch_id": batch_id, "packet": str(path), "item_ids": [i["item_id"] for i in chunk],
                        "inputs": [str(path)]})
    return {"batch_size": batch_size, "items": len(needs), "batches": batches}


def check(packet: dict, decisions: dict) -> list[str]:
    problems = []
    if decisions.get("batch_id") != packet["batch_id"]:
        problems.append(f"batch_id is {decisions.get('batch_id')!r}; this batch is {packet['batch_id']!r}")
    items = {item["item_id"]: item for item in packet["items"]}
    seen = []
    for n, decision in enumerate(decisions.get("decisions") or []):
        item_id = decision.get("item_id")
        where = f"decisions[{n}] ({item_id})"
        item = items.get(item_id)
        if item is None:
            problems.append(f"{where}: {item_id!r} is not an item in this batch")
            continue
        if item_id in seen:
            problems.append(f"{where}: {item_id} is decided more than once")
        seen.append(item_id)
        if decision.get("disposition") not in item["options"]:
            problems.append(f"{where}: disposition {decision.get('disposition')!r} is not one of the offered options "
                            f"{item['options']}")
        contract = packet["contracts"][item["carrier"]]
        known = {c["id"] for c in contract["clauses"]} | {"header"}
        unknown = [c for c in decision.get("clauses") or [] if c not in known]
        if unknown:
            problems.append(f"{where}: clauses {unknown} do not exist in {contract['file']}")
        text = str(decision.get("justification") or "").strip()
        if not text:
            problems.append(f"{where}: justification is empty")
        elif len(text) > MAX_JUSTIFICATION:
            problems.append(f"{where}: justification is {len(text)} characters (limit {MAX_JUSTIFICATION})")
        problems += grounding.check(text, grounding.fact_numbers(item, contract), f"{where} justification")
    missing = [i for i in items if i not in seen]
    if missing:
        problems.append(f"no decision for {missing}")
    return problems


def merge(batches: list[dict], decision_docs: list[dict], bundle: dict) -> dict:
    problems = []
    if len(batches) != len(decision_docs):
        raise AdjudicationError([f"{len(batches)} batches but {len(decision_docs)} decision files"])
    merged = {}
    for batch, doc in zip(batches, decision_docs):
        packet = json.loads(Path(batch["packet"]).read_text())
        problems += [f"{batch['batch_id']}: {p}" for p in check(packet, doc)]
        for decision in doc.get("decisions") or []:
            if decision.get("item_id") in merged:
                problems.append(f"{decision['item_id']} is decided in more than one batch")
            merged[decision.get("item_id")] = {
                "disposition": decision.get("disposition"),
                "justification": str(decision.get("justification") or "").strip(),
                "clauses": list(decision.get("clauses") or []),
                "batch_id": batch["batch_id"], "session_id": doc.get("_session_id")}
    expected = {item["item_id"] for item in bundle["planned"]["needs_judgement"]}
    if set(merged) != expected:
        problems.append(f"decisions cover {sorted(merged)}; policy left open {sorted(expected)}")
    if problems:
        raise AdjudicationError(problems)
    return {"decisions": merged}
