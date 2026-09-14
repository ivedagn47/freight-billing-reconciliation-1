"""Memos: one per line or finding that is not accepted, for the carrier-relations colleague.

    plan    one facts entry per non-accept report row (report + pricing detail + carrier name), batched
            into packets with the clause text of each contract involved
    check   (gate) every memo in the batch drafted exactly once; every field present and within its
            length; every figure copied from that memo's own facts or its contract
    render  every batch re-checked; one markdown file per memo. The facts table (amounts, disposition,
            clauses) is generated from the report by code; the agent contributes only prose.
"""

import json
import re
from pathlib import Path

from . import grounding
from .adjudication import load_contracts
from .report import inr
from .rules import write_json

FIELDS = {"headline": 140, "summary": 700, "contract_basis": 500, "action": 500}
SAFE = re.compile(r"[^A-Za-z0-9._-]+")


class MemoError(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def _safe(text: str) -> str:
    return SAFE.sub("-", text).strip("-")


def memo_facts(bundle: dict, report: dict, carriers_cfg: dict) -> list[dict]:
    priced = bundle["priced"]
    if len(report["lines"]) != len(priced["lines"]) or len(report["invoice_findings"]) != len(priced["invoice_findings"]):
        raise MemoError(["the report does not correspond to the priced bundle (row counts differ)"])
    carrier_of = {s["invoice"]: s["carrier"] for s in priced["invoices"]}
    facts = []
    for row, line in zip(report["lines"], priced["lines"]):
        if (row["invoice"], row["consignment_ref"]) != (line["invoice"], line["consignment_ref"]):
            raise MemoError([f"report row {row['invoice']}/{row['consignment_ref']} is out of order with pricing"])
        if row["disposition"] == "accept":
            continue
        facts.append({
            "memo_id": f"{_safe(line['invoice'])}-line-{line['line_no']:03d}", "kind": "line",
            "item_id": line["item_id"], "carrier": line["carrier"],
            "carrier_name": carriers_cfg[line["carrier"]]["name"], "invoice": row["invoice"],
            "doc_type": line["doc_type"], "line_no": line["line_no"], "consignment_ref": row["consignment_ref"],
            "shipment_id": row["shipment_id"], "billed_amount": row["billed_amount"],
            "expected_amount": row["expected_amount"], "delta": row["delta"], "disposition": row["disposition"],
            "justification": row["justification"], "contract_clause": row.get("contract_clause"),
            "notes": row.get("notes"),
            "flags": [{"code": f["code"], "message": f["message"], "details": f["details"]} for f in line["flags"]],
            "billed_charges": [{"label": c["label"], "code": c["code"], "amount": c["amount"]}
                               for c in line["billed_charges"]],
            "components": line["components"], "quantities": line["quantities"], "evidence": line["evidence"]})
    for row, finding in zip(report["invoice_findings"], priced["invoice_findings"]):
        if row["disposition"] == "accept":
            continue
        carrier = carrier_of[finding["invoice"]]
        suffix = "-".join(_safe(part) for part in finding["finding_id"].split("!")[2:])
        facts.append({
            "memo_id": f"{_safe(finding['invoice'])}-finding-{finding['code'].lower().replace('_', '-')}"
                       + (f"-{suffix}" if suffix else ""), "kind": "finding",
            "finding_id": finding["finding_id"], "carrier": carrier, "carrier_name": carriers_cfg[carrier]["name"],
            "invoice": row["invoice"], "code": finding["code"], "description": row["description"],
            "amount_impact": row["amount_impact"], "disposition": row["disposition"],
            "justification": row["justification"], "contract_clause": row.get("contract_clause"),
            "details": finding["details"]})
    ids = [f["memo_id"] for f in facts]
    if len(ids) != len(set(ids)):
        raise MemoError(["memo ids are not unique"])
    return facts


def plan(bundle: dict, report: dict, clauses_dir: Path, carriers_cfg: dict, batch_size: int, out_dir: Path) -> dict:
    if batch_size < 1:
        raise MemoError(["batch size must be at least 1"])
    facts = memo_facts(bundle, report, carriers_cfg)
    batches = []
    for start in range(0, len(facts), batch_size):
        chunk = facts[start:start + batch_size]
        batch_id = f"memo-{start // batch_size + 1:03d}"
        packet = {"batch_id": batch_id, "memos": chunk,
                  "contracts": load_contracts(clauses_dir, {f["carrier"] for f in chunk})}
        path = Path(out_dir).resolve() / "packets" / f"{batch_id}.json"
        write_json(path, packet)
        batches.append({"batch_id": batch_id, "packet": str(path), "memo_ids": [f["memo_id"] for f in chunk],
                        "inputs": [str(path)]})
    return {"batch_size": batch_size, "memos": len(facts), "batches": batches}


def check(packet: dict, drafts: dict) -> list[str]:
    problems = []
    if drafts.get("batch_id") != packet["batch_id"]:
        problems.append(f"batch_id is {drafts.get('batch_id')!r}; this batch is {packet['batch_id']!r}")
    facts = {f["memo_id"]: f for f in packet["memos"]}
    seen = []
    for n, memo in enumerate(drafts.get("memos") or []):
        memo_id = memo.get("memo_id")
        where = f"memos[{n}] ({memo_id})"
        fact = facts.get(memo_id)
        if fact is None:
            problems.append(f"{where}: {memo_id!r} is not a memo in this batch")
            continue
        if memo_id in seen:
            problems.append(f"{where}: drafted more than once")
        seen.append(memo_id)
        allowed = grounding.fact_numbers(fact, packet["contracts"][fact["carrier"]])
        for field, limit in FIELDS.items():
            text = str(memo.get(field) or "").strip()
            if not text:
                problems.append(f"{where}: {field} is empty")
            elif len(text) > limit:
                problems.append(f"{where}: {field} is {len(text)} characters (limit {limit})")
            problems += grounding.check(text, allowed, f"{where} {field}")
    missing = [m for m in facts if m not in seen]
    if missing:
        problems.append(f"no memo drafted for {missing}")
    return problems


def _amount(value, missing: str) -> str:
    return missing if value is None else inr(value)


def render_memo(fact: dict, memo: dict, session_id: str | None) -> str:
    rows = [("Disposition", f"**{fact['disposition'].capitalize()}**"),
            ("Carrier", f"{fact['carrier_name']} ({fact['carrier']})")]
    if fact["kind"] == "line":
        document = "Credit note" if fact["doc_type"] == "credit_note" else "Invoice"
        rows += [(document, f"{fact['invoice']}, line {fact['line_no']}"),
                 ("Consignment", fact["consignment_ref"]),
                 ("Shipment record", fact["shipment_id"] or "no matching shipment record"),
                 ("Billed", inr(fact["billed_amount"])),
                 ("Contract amount", _amount(fact["expected_amount"], "not determined")),
                 ("Difference (billed − contract)", _amount(fact["delta"], "not determined"))]
    else:
        rows += [("Invoice", fact["invoice"]), ("Finding", fact["code"]),
                 ("Amount impact", _amount(fact["amount_impact"], "not quantifiable"))]
    rows.append(("Contract clauses", fact.get("contract_clause") or "none cited"))
    table = "\n".join(f"| {k} | {v} |" for k, v in rows)
    return (f"# {memo['headline'].strip()}\n\n| | |\n|---|---|\n{table}\n\n"
            f"## What we found\n\n{memo['summary'].strip()}\n\n"
            f"## Contract basis\n\n{memo['contract_basis'].strip()}\n\n"
            f"## Next step\n\n{memo['action'].strip()}\n\n"
            f"---\n\nReconciliation justification: {fact['justification']}\n\n"
            f"<sub>Memo {fact['memo_id']}. Figures in the table come from the reconciliation report; the text was "
            f"drafted by worker session {session_id} and checked against the report's facts.</sub>\n")


def render(batches: list[dict], draft_docs: list[dict], out_dir: Path) -> dict:
    if len(batches) != len(draft_docs):
        raise MemoError([f"{len(batches)} batches but {len(draft_docs)} draft files"])
    problems, rendered = [], []
    out_dir = Path(out_dir).resolve()
    for batch, doc in zip(batches, draft_docs):
        packet = json.loads(Path(batch["packet"]).read_text())
        problems += [f"{batch['batch_id']}: {p}" for p in check(packet, doc)]
        if problems:
            continue
        facts = {f["memo_id"]: f for f in packet["memos"]}
        for memo in doc["memos"]:
            fact = facts[memo["memo_id"]]
            path = out_dir / f"{memo['memo_id']}.md"
            rendered.append((path, render_memo(fact, memo, doc.get("_session_id")),
                             {"memo_id": fact["memo_id"], "file": path.name, "kind": fact["kind"],
                              "invoice": fact["invoice"], "disposition": fact["disposition"],
                              "item": fact.get("item_id") or fact.get("finding_id"),
                              "batch_id": batch["batch_id"], "session_id": doc.get("_session_id")}))
    if problems:
        raise MemoError(problems)
    out_dir.mkdir(parents=True, exist_ok=True)
    stray = sorted(p.name for p in out_dir.iterdir() if p.name not in {r[0].name for r in rendered})
    if stray:
        raise MemoError([f"{out_dir} already contains files that are not memos of this run: {stray}"])
    for path, text, _ in rendered:
        path.write_text(text)
    return {"memos_dir": str(out_dir), "memos": [r[2] for r in rendered]}
