"""Report assembly: priced lines + policy decisions + adjudications -> reconciliation-report.json.

Amounts come only from pricing. Dispositions come from policy, or, for lines policy left open, from
an adjudication that must pick one of the offered options with a non-empty justification.
Before returning, the report is checked against report.schema.json and these invariants:

  - every in-scope line appears exactly once, in pricing order;
  - delta = billed - expected, and both are null together;
  - invoice totals and the summary recompute exactly from the lines and findings;
  - total_in_dispute counts each disputed rupee once: a line offset by a credit note is never
    disputed itself (its credit-note line carries the residual);
  - every disposition is allowed by policy.
"""

import json
from decimal import Decimal
from pathlib import Path

import jsonschema

from .money import to_decimal, to_number

INR_EPSILON = Decimal("0.005")


class AssemblyError(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def inr(value) -> str:
    return f"INR {to_decimal(value):,.2f}"


def clause_ref(contract_file: str | None, clauses: list[str]) -> str | None:
    if not contract_file or not clauses:
        return None
    ordered = sorted(set(clauses), key=lambda c: (c != "header", int(c) if c.isdigit() else 0))
    return f"{contract_file} " + ", ".join("header" if c == "header" else f"§{c}" for c in ordered)


def _messages(line: dict, codes: set[str] | None = None) -> str:
    return " ".join(f["message"].rstrip(".") + "." for f in line["flags"] if codes is None or f["code"] in codes)


def deterministic_justification(line: dict, decision: dict, policy: dict) -> str:
    effects = {f["code"]: policy["line_flags"][f["code"]] for f in line["flags"]}
    escalating = {c for c, e in effects.items() if e == "escalate"}
    billed, expected, delta = line["billed_amount"], line["expected_amount"], line["delta"]
    if decision["disposition"] == "escalate":
        return "Escalated: " + (_messages(line, escalating) or "the contract amount could not be determined.")
    if line["doc_type"] == "credit_note":
        original = next((f["details"] for f in line["flags"] if f["code"] == "CREDIT_NOTE"), {})
        text = (f"Credit of {inr(billed)} against {original.get('original_item')}; the contract correction for that "
                f"line is {inr(expected)}, leaving {inr(delta)}.")
    elif any(e == "offset" for e in effects.values()):
        text = (f"Billed {inr(billed)} against a contract amount of {inr(expected)} ({inr(delta)}); "
                + _messages(line, {c for c, e in effects.items() if e == "offset"}))
    elif to_decimal(delta) > to_decimal(str(policy["tolerance_inr"])):
        text = f"Billed {inr(billed)} exceeds the contract amount of {inr(expected)} by {inr(delta)}."
    elif to_decimal(delta) < -to_decimal(str(policy["tolerance_inr"])):
        text = (f"Billed {inr(billed)} is below the contract amount of {inr(expected)}; accepted at the billed "
                "amount (the expected amount is unchanged).")
    else:
        text = f"Billed {inr(billed)} matches the contract amount of {inr(expected)}."
    detail = _messages(line, {c for c, e in effects.items() if e in ("dispute", "info") and c != "CREDIT_NOTE"})
    return f"{text} {detail}".strip()


def _check_adjudications(needs_judgement: list[dict], adjudications: dict) -> list[str]:
    problems = []
    expected_ids = {n["item_id"] for n in needs_judgement}
    for extra in sorted(set(adjudications) - expected_ids):
        problems.append(f"adjudication for {extra}, which policy already decided")
    for item in needs_judgement:
        adj = adjudications.get(item["item_id"])
        if adj is None:
            problems.append(f"{item['item_id']}: missing adjudication (options {item['options']})")
            continue
        if adj.get("disposition") not in item["options"]:
            problems.append(f"{item['item_id']}: disposition {adj.get('disposition')!r} is not one of {item['options']}")
        if not str(adj.get("justification") or "").strip():
            problems.append(f"{item['item_id']}: adjudication has no justification")
    return problems


def assemble(priced: dict, planned: dict, policy: dict, adjudications: dict | None = None) -> dict:
    adjudications = adjudications or {}
    problems = _check_adjudications(planned["needs_judgement"], adjudications)
    if problems:
        raise AssemblyError(problems)

    lines = []
    for line in priced["lines"]:
        decision = planned["decisions"][line["item_id"]]
        clause = clause_ref(line["contract_file"], line["contract_clauses"])
        if decision["disposition"] is None:
            adj = adjudications[line["item_id"]]
            disposition, justification = adj["disposition"], adj["justification"].strip()
            cited = clause_ref(line["contract_file"], list(adj.get("clauses") or []))
            if cited:
                justification = f"{justification} (cited: {cited})"
        else:
            disposition, justification = decision["disposition"], deterministic_justification(line, decision, policy)
        row = {"invoice": line["invoice"], "consignment_ref": line["consignment_ref"], "shipment_id": line["shipment_id"],
               "billed_amount": to_number(line["billed_amount"]),
               "expected_amount": None if line["expected_amount"] is None else to_number(line["expected_amount"]),
               "delta": None if line["delta"] is None else to_number(line["delta"]),
               "disposition": disposition, "justification": justification, "contract_clause": clause}
        notes = _messages(line, {f["code"] for f in line["flags"] if policy["line_flags"][f["code"]] == "info"
                                 and f["code"] not in ("CREDIT_NOTE",)})
        if notes:
            row["notes"] = notes
        lines.append(row)

    findings = []
    for f in priced["invoice_findings"]:
        disposition = planned["decisions"][f["finding_id"]]["disposition"]
        findings.append({"invoice": f["invoice"], "description": f["description"],
                         "amount_impact": None if f["amount_impact"] is None else to_number(f["amount_impact"]),
                         "disposition": disposition,
                         "justification": f"{f['description'][0].upper()}{f['description'][1:]}.",
                         "contract_clause": clause_ref(_contract_file(priced, f["invoice"]), f["clauses"])})

    totals = [{"invoice": s["invoice"], "billed_total": to_number(s["billed_total"]),
               "expected_total": None if s["expected_total"] is None else to_number(s["expected_total"])}
              for s in priced["invoices"]]
    in_dispute = sum((abs(to_decimal(l["delta"])) for l, row in zip(priced["lines"], lines)
                      if row["disposition"] == "dispute"), Decimal(0))
    in_dispute += sum((abs(to_decimal(f["amount_impact"])) for f, row in zip(priced["invoice_findings"], findings)
                       if row["disposition"] == "dispute" and f["amount_impact"] is not None), Decimal(0))
    expected_totals = [s["expected_total"] for s in priced["invoices"]]
    summary = {
        "total_billed": to_number(sum((to_decimal(s["billed_total"]) for s in priced["invoices"]), Decimal(0))),
        "total_expected": None if any(v is None for v in expected_totals)
        else to_number(sum((to_decimal(v) for v in expected_totals), Decimal(0))),
        "total_in_dispute": to_number(in_dispute),
        "line_count": len(lines),
        "counts_by_disposition": {d: sum(1 for l in lines if l["disposition"] == d) for d in ("accept", "dispute", "escalate")},
    }
    return {"lines": lines, "invoice_findings": findings, "invoice_totals": totals, "summary": summary}


def _contract_file(priced: dict, invoice: str) -> str | None:
    return next((l["contract_file"] for l in priced["lines"] if l["invoice"] == invoice), None)


def verify(report: dict, priced: dict, planned: dict, schema_path: Path, in_scope_lines: int | None = None) -> None:
    """Raise AssemblyError unless the report satisfies the schema and every invariant."""
    problems = [f"schema /{'/'.join(str(p) for p in e.absolute_path)}: {e.message}" for e in
                jsonschema.Draft7Validator(json.loads(Path(schema_path).read_text())).iter_errors(report)]

    lines = report["lines"]
    if len(lines) != len(priced["lines"]):
        problems.append(f"report has {len(lines)} lines, pricing has {len(priced['lines'])}")
    if in_scope_lines is not None and len(lines) != in_scope_lines:
        problems.append(f"report has {len(lines)} lines, the in-scope documents have {in_scope_lines}")
    keys = [(l["invoice"], l["consignment_ref"], p["line_no"]) for l, p in zip(lines, priced["lines"])]
    if len(set(keys)) != len(keys):
        problems.append("a line appears more than once")

    for row, line in zip(lines, priced["lines"]):
        where = line["item_id"]
        if (row["invoice"], row["consignment_ref"]) != (line["invoice"], line["consignment_ref"]):
            problems.append(f"{where}: out of order or mismatched")
        if (row["expected_amount"] is None) != (row["delta"] is None):
            problems.append(f"{where}: expected_amount and delta must be null together")
        if row["delta"] is not None and abs(to_decimal(row["billed_amount"]) - to_decimal(row["expected_amount"])
                                            - to_decimal(row["delta"])) > INR_EPSILON:
            problems.append(f"{where}: delta is not billed - expected")
        decision = planned["decisions"][where]
        allowed = decision["options"] or [decision["disposition"]]
        if row["disposition"] not in allowed:
            problems.append(f"{where}: disposition {row['disposition']} is not allowed by policy ({allowed})")
        if any(f["code"] == "CREDIT_NOTE_OFFSET" for f in line["flags"]) and row["disposition"] == "dispute":
            problems.append(f"{where}: offset by a credit note but disputed; its difference would be counted twice")

    billed = {t["invoice"]: to_decimal(t["billed_total"]) for t in report["invoice_totals"]}
    for summary in priced["invoices"]:
        if billed.get(summary["invoice"]) != to_decimal(summary["billed_total"]):
            problems.append(f"invoice total for {summary['invoice']} does not match the document")
    if {l["invoice"] for l in lines} - set(billed):
        problems.append("a line's invoice has no invoice_totals entry")

    s = report["summary"]
    if s["line_count"] != len(lines):
        problems.append("summary.line_count does not match lines")
    if s["counts_by_disposition"] != {d: sum(1 for l in lines if l["disposition"] == d)
                                      for d in ("accept", "dispute", "escalate")}:
        problems.append("summary.counts_by_disposition does not match lines")
    if abs(to_decimal(s["total_billed"]) - sum((to_decimal(v) for v in billed.values()), Decimal(0))) > INR_EPSILON:
        problems.append("summary.total_billed does not equal the invoice totals")
    disputed = sum((abs(to_decimal(l["delta"])) for l in lines if l["disposition"] == "dispute"), Decimal(0)) + \
        sum((abs(to_decimal(f["amount_impact"])) for f in report["invoice_findings"]
             if f["disposition"] == "dispute" and f["amount_impact"] is not None), Decimal(0))
    if abs(to_decimal(s["total_in_dispute"]) - disputed) > INR_EPSILON:
        problems.append("summary.total_in_dispute does not equal the disputed deltas and findings")
    if problems:
        raise AssemblyError(problems)


def memo_items(report: dict) -> list[dict]:
    """Every line and finding that needs a memo (anything other than accept)."""
    items = [{"kind": "line", "invoice": l["invoice"], "consignment_ref": l["consignment_ref"],
              "disposition": l["disposition"]} for l in report["lines"] if l["disposition"] != "accept"]
    items += [{"kind": "finding", "invoice": f["invoice"], "description": f["description"],
               "disposition": f["disposition"]} for f in report["invoice_findings"] if f["disposition"] != "accept"]
    return items
