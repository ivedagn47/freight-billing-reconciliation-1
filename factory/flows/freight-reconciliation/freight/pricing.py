"""Pricing: apply a verified rate spec to shipment records and compare with what was billed.

Prices come only from shipment records (the ground truth) and the carrier's rate spec. What the
invoice states about weight, distance or service is compared as evidence, never used to price.
Everything is exact Decimal arithmetic; a line's expected amount is rounded once, half-up.

For every in-scope line the output records billed and expected amounts, the delta, a
component breakdown, the clauses behind the expected amount, and generic flags describing
anything that needs attention. Dispositions are not decided here (see policy.py).

Credit notes (rule confirmed before implementation): a credit line's expected amount is the
correction the contract entitles BlueFin to (original expected - original billed, less credits
already issued against that line); its delta is credit issued minus that correction. The
original line keeps its own delta and is flagged CREDIT_NOTE_OFFSET so policy lets the credit
line carry the residual and no rupee is counted twice.
"""

from collections import defaultdict
from datetime import date
from decimal import Decimal

from .money import fmt, round_inr, to_decimal
from .ratespec import in_band

LINE_FLAG_CODES = {
    "NO_SHIPMENT_MATCH", "SHIPMENT_AMBIGUOUS", "SHIPMENT_DATA_MISSING", "CARRIER_MISMATCH", "OUTSIDE_TERM",
    "CONTRACT_GAP", "OUTSIDE_RATE_CARD", "UNRECOGNIZED_CHARGE", "CREDIT_NOTE_UNMATCHED", "CREDIT_NOTE_UNDETERMINED",
    "DUPLICATE_BILLING", "UNCONTRACTED_CHARGE", "SERVICE_NOT_OFFERED", "NOT_DELIVERED", "COMPONENT_ARITHMETIC",
    "CREDIT_NOTE_OFFSET", "CREDIT_NOTE", "CHARGE_NOT_APPLICABLE", "ATTRIBUTE_MISMATCH", "UNDERBILLED",
    "CHARGE_UNVERIFIABLE", "CONDITION_UNVERIFIABLE",
}
FINDING_CODES = {"ADJUSTMENT_MISMATCH", "ADJUSTMENT_UNDETERMINED", "INVOICE_TOTAL_MISMATCH"}

LINE_ARITHMETIC_CHECKS = {"line_total_equals_charges", "line_amount_equals_rate_x_weight_plus_handling"}
TOTAL_CHECKS = {"document_total_equals_lines", "document_total_equals_lines_less_discount"}
ATTRIBUTE_PAIRS = (("weight_kg", "billed_weight_kg"), ("actual_weight_kg", "billed_weight_kg"),
                   ("distance_km", "distance_km"), ("service_level", "service_level"))
SHIPMENT_EVIDENCE = ("shipment_id", "ship_date", "billed_weight_kg", "distance_km", "service_level",
                     "special_handling", "delivery_status", "delivered_at")


class PricingError(Exception):
    pass


class _MissingData(Exception):
    pass


def flag(flag_code: str, message: str, clauses: list[str] | None = None, **details) -> dict:
    assert flag_code in LINE_FLAG_CODES or flag_code in FINDING_CODES, flag_code
    return {"code": flag_code, "message": message, "clauses": list(clauses or []), "details": details}


def item_id(doc_id: str, line_no: int) -> str:
    return f"{doc_id}#{line_no}"


# ---------------------------------------------------------------- rate spec evaluation

def _condition(cond: dict, shipment: dict) -> bool | None:
    """True or False, or None when the answer depends on a fact the shipment record does not hold
    (three-valued: `all` is False if any part is False, `any` is True if any part is True)."""
    if "service_level" in cond:
        return shipment.get("service_level") == cond["service_level"]
    if "special_handling_includes" in cond:
        return cond["special_handling_includes"] in (shipment.get("special_handling") or [])
    if "unrecorded" in cond:
        return None
    if "all" in cond:
        values = [_condition(c, shipment) for c in cond["all"]]
        return False if any(v is False for v in values) else (None if None in values else True)
    if "any" in cond:
        values = [_condition(c, shipment) for c in cond["any"]]
        return True if any(v is True for v in values) else (None if None in values else False)
    value = _condition(cond["not"], shipment)
    return None if value is None else not value


def _unrecorded(cond: dict) -> list[str]:
    if "unrecorded" in cond:
        return [cond["unrecorded"]]
    if "not" in cond:
        return _unrecorded(cond["not"])
    return [d for sub in cond.get("all") or cond.get("any") or [] for d in _unrecorded(sub)]


def _value(ref: dict, shipment: dict, quantities: dict) -> Decimal:
    if "field" in ref:
        raw = shipment.get(ref["field"])
        if raw is None:
            raise _MissingData(ref["field"])
        return to_decimal(raw)
    if "quantity" in ref:
        return quantities[ref["quantity"]]
    return to_decimal(ref["const"])


def _gap(component: dict, value: Decimal) -> dict:
    bands = component["calc"]["bands"]

    def below(b):
        return b["min"] is not None and (value < to_decimal(b["min"]) or
                                         (value == to_decimal(b["min"]) and not b["min_inclusive"]))

    def above(b):
        return b["max"] is not None and (value > to_decimal(b["max"]) or
                                         (value == to_decimal(b["max"]) and not b["max_inclusive"]))

    if all(below(b) for b in bands) or all(above(b) for b in bands):
        return flag("OUTSIDE_RATE_CARD", f"the {component['kind']} charge: {value} is outside every rate band the "
                    "contract states", component["clauses"], component=component["name"], value=str(value))
    return flag("CONTRACT_GAP", f"the {component['kind']} charge: no rate band covers {value}; the contract does not "
                "determine this price", component["clauses"], component=component["name"], value=str(value))


def evaluate(spec: dict, shipment: dict) -> dict:
    """Price one shipment. amount is None when the spec cannot determine it (see `flags`)."""
    quantities: dict[str, Decimal] = {}
    clauses: list[str] = []
    flags: list[dict] = []
    rows: list[dict] = []
    try:
        for q in spec["quantities"]:
            values = [_value(a, shipment, quantities) for a in q["args"]]
            quantities[q["name"]] = max(values) if q["op"] == "max" else min(values)

        amounts: dict[str, Decimal | None] = {}
        unverifiable: set[str] = set()
        for c in spec["components"]:
            applies = True if "when" not in c else _condition(c["when"], shipment)
            row = {"name": c["name"], "kind": c["kind"], "applied": applies is True, "amount": None,
                   "clauses": c["clauses"]}
            if applies is None:  # excluded from the amount: the records cannot establish it applies
                row["unverifiable"] = _unrecorded(c["when"])
                unverifiable.add(c["name"])
            rows.append(row)
            if applies is not True:
                amounts[c["name"]] = Decimal(0)
                continue
            calc, amount = c["calc"], None
            if calc["op"] == "flat":
                amount = to_decimal(calc["amount"])
            elif calc["op"] == "per_unit":
                amount = _value(calc["basis"], shipment, quantities) * to_decimal(calc["rate"])
            elif calc["op"] == "banded_rate":
                selector = _value(calc["select_by"], shipment, quantities)
                matches = [b for b in calc["bands"] if in_band(selector, b)]
                if matches:
                    amount = _value(calc["basis"], shipment, quantities) * to_decimal(matches[0]["rate"])
                    row["band"] = matches[0]
                else:
                    flags.append(_gap(c, selector))
            else:
                depends = [name for name in calc["of"] if name in unverifiable]
                parts = [amounts[name] for name in calc["of"]]
                if depends:
                    flags.append(flag("CONDITION_UNVERIFIABLE", f"the {c['kind']} charge is a percentage of charges "
                                      "whose condition depends on facts the shipment record does not hold",
                                      c["clauses"], component=c["name"], depends_on=depends))
                elif all(p is not None for p in parts):
                    amount = sum(parts, Decimal(0)) * to_decimal(calc["percent"]) / Decimal(100)
            amounts[c["name"]] = amount
            row["amount"] = None if amount is None else str(amount)
            clauses += [x for x in c["clauses"] if x not in clauses]
    except _MissingData as missing:
        flags.append(flag("SHIPMENT_DATA_MISSING", f"the shipment record has no {missing.args[0]}", [],
                          field=missing.args[0]))
        return {"amount": None, "components": rows, "quantities": {k: str(v) for k, v in quantities.items()},
                "clauses": clauses, "flags": flags}

    for q in spec["quantities"]:
        clauses += [x for x in q["clauses"] if x not in clauses]
    applied = [amounts[r["name"]] for r in rows if r["applied"]]
    total = None if any(a is None for a in applied) else round_inr(sum(applied, Decimal(0)))
    return {"amount": total, "components": rows, "quantities": {k: str(v) for k, v in quantities.items()},
            "clauses": clauses, "flags": flags}


# ---------------------------------------------------------------- lines

def _order_key(doc: dict, line: dict) -> tuple:
    period = doc["billing_period"] or (doc["issue_date"] or "9999-99")[:7]
    return (period, (doc.get("period") or {}).get("start") or "", doc["doc_id"], line["line_no"])


def first_billing(docs: list[dict]) -> dict[tuple[str, str], str]:
    """(carrier, consignment_ref) -> item id of the earliest invoice line billing it, across all periods."""
    first: dict[tuple[str, str], tuple] = {}
    for doc in docs:
        if doc["doc_type"] != "invoice":
            continue
        for line in doc["lines"]:
            key = (doc["carrier"], line["consignment_ref"])
            candidate = (_order_key(doc, line), item_id(doc["doc_id"], line["line_no"]))
            if key not in first or candidate < first[key]:
                first[key] = candidate
    return {k: v[1] for k, v in first.items()}


def _charge_flags(line: dict, spec: dict, components: list[dict]) -> tuple[list[dict], list[dict]]:
    applied = {r["name"]: r["applied"] for r in components}
    unverifiable = {r["name"]: r["unverifiable"] for r in components if r.get("unverifiable")}
    billed, flags = [], []
    for charge in line["charges"]:
        matched = [c for c in spec["components"] if charge["code"] in c["charge_codes"]]
        row = {**charge, "matched_components": [c["name"] for c in matched]}
        billed.append(row)
        if charge["code"] == "other":
            flags.append(flag("UNRECOGNIZED_CHARGE", f"charge {charge['label']!r} could not be classified",
                              label=charge["label"], amount=charge["amount"]))
        elif not matched:
            flags.append(flag("UNCONTRACTED_CHARGE", f"{charge['label']} ({charge['code']}) is not a charge the "
                              "contract provides for", label=charge["label"], code=charge["code"],
                              amount=charge["amount"]))
        elif components and not any(applied.get(c["name"]) for c in matched) and \
                any(c["name"] in unverifiable for c in matched):
            pending = [c for c in matched if c["name"] in unverifiable]
            facts = sorted({d for c in pending for d in unverifiable[c["name"]]})
            flags.append(flag("CHARGE_UNVERIFIABLE", f"{charge['label']} is billed, but whether the contract allows it "
                              f"depends on facts the shipment record does not hold: {'; '.join(facts)}",
                              sorted({x for c in pending for x in c["clauses"]}), label=charge["label"],
                              amount=charge["amount"], components=[c["name"] for c in pending], unrecorded=facts))
        elif components and not any(applied.get(c["name"]) for c in matched):
            flags.append(flag("CHARGE_NOT_APPLICABLE", f"{charge['label']} is billed but the contract condition for it "
                              "is not met by the shipment record", sorted({x for c in matched for x in c["clauses"]}),
                              label=charge["label"], amount=charge["amount"],
                              components=[c["name"] for c in matched]))
    return billed, flags


def _attribute_flags(line: dict, shipment: dict) -> list[dict]:
    mismatches = []
    for invoice_key, shipment_key in ATTRIBUTE_PAIRS:
        if invoice_key not in line["attributes"] or shipment.get(shipment_key) is None:
            continue
        stated, recorded = line["attributes"][invoice_key], shipment[shipment_key]
        same = (to_decimal(stated) == to_decimal(recorded)) if not isinstance(recorded, str) else stated == recorded
        if not same:
            mismatches.append({"invoice_field": invoice_key, "invoice": stated, "shipment_field": shipment_key,
                               "shipment": recorded})
    if not mismatches:
        return []
    return [flag("ATTRIBUTE_MISMATCH", "the invoice states "
                 + ", ".join(f"{m['invoice_field']}={m['invoice']} (shipment record: {m['shipment']})" for m in mismatches),
                 mismatches=mismatches)]


def price_invoice_line(doc: dict, line: dict, spec: dict, shipments_by_ref: dict, first_billed: dict,
                       tolerance: Decimal) -> dict:
    iid = item_id(doc["doc_id"], line["line_no"])
    billed = to_decimal(line["stated_total"])
    flags: list[dict] = []
    shipment, expected, evaluation = None, None, None
    candidates = shipments_by_ref.get(line["consignment_ref"], [])

    if not candidates:
        flags.append(flag("NO_SHIPMENT_MATCH", f"no shipment record has consignment reference {line['consignment_ref']}"))
    elif len(candidates) > 1:
        flags.append(flag("SHIPMENT_AMBIGUOUS", f"{len(candidates)} shipment records share consignment reference "
                          f"{line['consignment_ref']}", shipment_ids=[s["shipment_id"] for s in candidates]))
    else:
        shipment = candidates[0]
        if shipment["carrier"] != doc["carrier"]:
            flags.append(flag("CARRIER_MISMATCH", f"shipment {shipment['shipment_id']} belongs to carrier "
                              f"{shipment['carrier']}, not {doc['carrier']}"))

    primary = first_billed.get((doc["carrier"], line["consignment_ref"]))
    duplicate = primary is not None and primary != iid
    if duplicate:
        flags.append(flag("DUPLICATE_BILLING", f"{line['consignment_ref']} was already billed on {primary}",
                          first_billed_on=primary))

    if shipment is not None and not any(f["code"] == "CARRIER_MISMATCH" for f in flags):
        term = spec["term"]
        ship_day = date.fromisoformat(shipment["ship_date"])
        if not date.fromisoformat(term["start"]) <= ship_day <= date.fromisoformat(term["end"]):
            flags.append(flag("OUTSIDE_TERM", f"ship date {shipment['ship_date']} is outside the agreement term "
                              f"{term['start']} to {term['end']}", term["clauses"]))
        else:
            evaluation = evaluate(spec, shipment)
            flags += evaluation["flags"]
            expected = Decimal(0) if duplicate else evaluation["amount"]
        if shipment.get("service_level") not in spec["service_levels"]["allowed"]:
            flags.append(flag("SERVICE_NOT_OFFERED", f"service level {shipment.get('service_level')!r} is not offered "
                              "under the agreement", spec["service_levels"]["clauses"],
                              service_level=shipment.get("service_level")))
        if shipment.get("delivery_status") != "delivered":
            flags.append(flag("NOT_DELIVERED", f"the shipment record's delivery status is "
                              f"{shipment.get('delivery_status')!r}", delivery_status=shipment.get("delivery_status")))
        flags += _attribute_flags(line, shipment)

    billed_charges, charge_flags = _charge_flags(line, spec, evaluation["components"] if evaluation else [])
    flags += charge_flags
    for failure in doc["integrity"]["failures"]:
        if failure["check"] in LINE_ARITHMETIC_CHECKS and failure["line_no"] == line["line_no"]:
            flags.append(flag("COMPONENT_ARITHMETIC", f"on the invoice itself, {failure['message']}",
                              check=failure["check"], computed=failure["expected"], stated=failure["actual"]))

    delta = None if expected is None else billed - expected
    if delta is not None and delta < -tolerance:
        flags.append(flag("UNDERBILLED", f"billed {fmt(billed)} is below the contract amount {fmt(expected)}"))
    return {
        "item_id": iid, "invoice": doc["doc_id"], "doc_type": "invoice", "carrier": doc["carrier"],
        "line_no": line["line_no"], "consignment_ref": line["consignment_ref"],
        "shipment_id": shipment["shipment_id"] if shipment else None,
        "billed_amount": fmt(billed), "expected_amount": None if expected is None else fmt(expected),
        "delta": None if delta is None else fmt(delta),
        "contract_file": spec["contract_file"],
        # a later billing of a consignment expects nothing; no contract clause is what makes that amount zero
        "contract_clauses": [] if duplicate else (evaluation or {}).get("clauses", []),
        "components": (evaluation or {}).get("components", []),
        "quantities": (evaluation or {}).get("quantities", {}),
        "billed_charges": billed_charges,
        "flags": flags,
        "evidence": {"invoice_attributes": line["attributes"], "note": line["note"], "source": line["source"],
                     "shipment": {k: shipment.get(k) for k in SHIPMENT_EVIDENCE} if shipment else None},
    }


def price_credit_line(doc: dict, line: dict, originals: dict, credits_issued: dict, tolerance: Decimal) -> dict:
    iid = item_id(doc["doc_id"], line["line_no"])
    credit = to_decimal(line["stated_total"])
    flags: list[dict] = []
    expected, clauses, shipment_id = None, [], None
    original = originals.get((doc["against_invoice"], line["consignment_ref"]))
    if original is None:
        flags.append(flag("CREDIT_NOTE_UNMATCHED", f"{doc['against_invoice']} has no in-scope line for "
                          f"{line['consignment_ref']}", against_invoice=doc["against_invoice"]))
    else:
        shipment_id, clauses = original["shipment_id"], original["contract_clauses"]
        prior = credits_issued[original["item_id"]]
        if original["expected_amount"] is None:
            flags.append(flag("CREDIT_NOTE_UNDETERMINED", f"the contract amount for {original['item_id']} is "
                              "undetermined, so the correction it entitles BlueFin to cannot be computed",
                              original_item=original["item_id"]))
        else:
            expected = round_inr(to_decimal(original["expected_amount"]) - to_decimal(original["billed_amount"]) - prior)
        flags.append(flag("CREDIT_NOTE", f"credit against {original['item_id']}", clauses,
                          original_item=original["item_id"], original_billed=original["billed_amount"],
                          original_expected=original["expected_amount"], earlier_credits=fmt(prior)))
        original["flags"].append(flag("CREDIT_NOTE_OFFSET", f"corrected by credit note line {iid} "
                                      f"({fmt(credit)}); the residual difference is carried on that line",
                                      credit_item=iid, credit_amount=fmt(credit)))
        credits_issued[original["item_id"]] += credit
    delta = None if expected is None else credit - expected
    if delta is not None and delta < -tolerance:
        flags.append(flag("UNDERBILLED", f"the credit {fmt(credit)} exceeds the correction the contract requires "
                          f"({fmt(expected)})"))
    return {
        "item_id": iid, "invoice": doc["doc_id"], "doc_type": "credit_note", "carrier": doc["carrier"],
        "line_no": line["line_no"], "consignment_ref": line["consignment_ref"], "shipment_id": shipment_id,
        "billed_amount": fmt(credit), "expected_amount": None if expected is None else fmt(expected),
        "delta": None if delta is None else fmt(delta), "contract_file": original["contract_file"] if original else None,
        "contract_clauses": clauses, "components": [], "quantities": {},
        "billed_charges": [{**c, "matched_components": []} for c in line["charges"]], "flags": flags,
        "evidence": {"invoice_attributes": line["attributes"], "note": line["note"], "source": line["source"],
                     "against_invoice": doc["against_invoice"], "issue_date": doc["issue_date"], "shipment": None},
    }


# ---------------------------------------------------------------- invoice level

def adjustment_terms(adj: dict) -> tuple[str, str]:
    """(description, id) of an invoice adjustment, from what it does rather than the name a rate spec gave it,
    so agreeing extractions, and separate runs, describe and identify the same term identically. Count
    conditions are stated as the equivalent inclusive bound ("more than 12" is "13 or more")."""
    (op, bound), = adj["when"]["consignments_in_billing_month"].items()
    percent = format(to_decimal(adj["calc"]["percent"]).normalize(), "f")
    if op in ("gt", "gte"):
        least = bound + 1 if op == "gt" else bound
        return (f"{percent}% {adj['kind']} for {least} or more consignments in the billing month",
                f"{adj['kind']}-{percent}pct-from-{least}")
    most = bound - 1 if op == "lt" else bound
    return (f"{percent}% {adj['kind']} for {most} or fewer consignments in the billing month",
            f"{adj['kind']}-{percent}pct-upto-{most}")


def _count_condition(condition: dict, count: int) -> bool:
    op, bound = next(iter(condition.items()))
    return {"gt": count > bound, "gte": count >= bound, "lt": count < bound, "lte": count <= bound}[op]


def invoice_level(doc: dict, spec: dict, priced_lines: list[dict], shipments: list[dict],
                  tolerance: Decimal) -> tuple[dict, list[dict]]:
    expected_values = [p["expected_amount"] for p in priced_lines]
    lines_expected = None if any(v is None for v in expected_values) else \
        sum((to_decimal(v) for v in expected_values), Decimal(0))
    findings, adjustments = [], []
    adjustments_expected = Decimal(0)
    stated_discount = to_decimal(doc["stated"].get("discount") or 0)

    for adj in spec["invoice_adjustments"]:
        month = doc["billing_period"]
        count = sum(1 for s in shipments if s["carrier"] == doc["carrier"] and s["ship_date"][:7] == month)
        applies = _count_condition(adj["when"]["consignments_in_billing_month"], count)
        label, slug = adjustment_terms(adj)
        amount = Decimal(0)
        if applies:
            amount = None if lines_expected is None else \
                round_inr(lines_expected * to_decimal(adj["calc"]["percent"]) / Decimal(100))
        adjustments.append({"name": slug, "description": label, "kind": adj["kind"], "applies": applies,
                            "consignments_in_billing_month": count, "expected_amount": None if amount is None else fmt(amount),
                            "clauses": adj["clauses"]})
        if amount is None:
            adjustments_expected = None
            findings.append({"finding_id": f"{doc['doc_id']}!ADJUSTMENT_UNDETERMINED!{slug}", "invoice": doc["doc_id"],
                             "code": "ADJUSTMENT_UNDETERMINED", "direction": "undetermined", "amount_impact": None,
                             "description": f"the contract's {label} applies ({count} consignments in {month}) but the "
                                            "invoice's contract total is undetermined", "clauses": adj["clauses"],
                             "details": {"adjustment": slug, "consignments_in_billing_month": count}})
            continue
        if adjustments_expected is not None:
            adjustments_expected += amount
        impact = amount - stated_discount
        if abs(impact) > tolerance:
            findings.append({"finding_id": f"{doc['doc_id']}!ADJUSTMENT_MISMATCH!{slug}", "invoice": doc["doc_id"],
                             "code": "ADJUSTMENT_MISMATCH", "direction": "over" if impact > 0 else "under",
                             "amount_impact": fmt(impact),
                             "description": f"the contract's {label} entitles BlueFin to {fmt(amount)} "
                                            f"({count} consignments in {month}); the invoice deducts {fmt(stated_discount)}",
                             "clauses": adj["clauses"],
                             "details": {"adjustment": slug, "expected": fmt(amount),
                                         "invoiced": fmt(stated_discount), "consignments_in_billing_month": count}})

    for failure in doc["integrity"]["failures"]:
        if failure["check"] in TOTAL_CHECKS:
            impact = to_decimal(failure["actual"]) - to_decimal(failure["expected"])
            findings.append({"finding_id": f"{doc['doc_id']}!INVOICE_TOTAL_MISMATCH", "invoice": doc["doc_id"],
                             "code": "INVOICE_TOTAL_MISMATCH", "direction": "over" if impact > 0 else "under",
                             "amount_impact": fmt(impact),
                             "description": f"the stated total {failure['actual']} differs from the invoice's own lines "
                                            f"({failure['expected']})", "clauses": [], "details": dict(failure)})

    expected_total = None if lines_expected is None or adjustments_expected is None else \
        round_inr(lines_expected - adjustments_expected)
    summary = {"invoice": doc["doc_id"], "doc_type": doc["doc_type"], "carrier": doc["carrier"],
               "billed_total": doc["stated"]["total"],
               "expected_lines_total": None if lines_expected is None else fmt(lines_expected),
               "adjustments": adjustments, "expected_total": None if expected_total is None else fmt(expected_total),
               "items": [p["item_id"] for p in priced_lines]}
    return summary, findings


def price(docs: list[dict], in_scope: list[str], shipments: list[dict], specs: dict,
          tolerance: str = "0.01") -> dict:
    tol = to_decimal(tolerance)
    by_id = {d["doc_id"]: d for d in docs}
    unknown = [i for i in in_scope if i not in by_id]
    if unknown:
        raise PricingError(f"in-scope documents were not parsed: {unknown}")
    scope = [by_id[i] for i in in_scope]
    missing_specs = sorted({d["carrier"] for d in scope} - set(specs))
    if missing_specs:
        raise PricingError(f"no rate spec for carriers {missing_specs}")
    mislabelled = sorted(c for c, s in specs.items() if s["carrier"] != c)
    if mislabelled:
        raise PricingError(f"rate specs supplied for {mislabelled} name a different carrier")

    shipments_by_ref = defaultdict(list)
    for s in shipments:
        shipments_by_ref[s["carrier_consignment_ref"]].append(s)
    first_billed = first_billing(docs)

    invoices = sorted((d for d in scope if d["doc_type"] == "invoice"), key=lambda d: (d["carrier"], d["doc_id"]))
    credit_notes = sorted((d for d in scope if d["doc_type"] == "credit_note"),
                          key=lambda d: (d["carrier"], d["issue_date"] or "", d["doc_id"]))
    lines, per_invoice = [], {}
    for doc in invoices:
        spec = specs[doc["carrier"]]
        per_invoice[doc["doc_id"]] = [price_invoice_line(doc, line, spec, shipments_by_ref, first_billed, tol)
                                      for line in doc["lines"]]
        lines += per_invoice[doc["doc_id"]]

    originals = {}
    for p in lines:
        originals.setdefault((p["invoice"], p["consignment_ref"]), p)
    credits_issued = defaultdict(Decimal)
    for doc in credit_notes:
        per_invoice[doc["doc_id"]] = [price_credit_line(doc, line, originals, credits_issued, tol) for line in doc["lines"]]
        lines += per_invoice[doc["doc_id"]]

    summaries, findings = [], []
    for doc in invoices + credit_notes:
        if doc["doc_type"] == "invoice":
            summary, doc_findings = invoice_level(doc, specs[doc["carrier"]], per_invoice[doc["doc_id"]], shipments, tol)
        else:
            values = [p["expected_amount"] for p in per_invoice[doc["doc_id"]]]
            total = None if any(v is None for v in values) else fmt(sum((to_decimal(v) for v in values), Decimal(0)))
            summary = {"invoice": doc["doc_id"], "doc_type": "credit_note", "carrier": doc["carrier"],
                       "billed_total": doc["stated"]["total"], "expected_lines_total": total, "adjustments": [],
                       "expected_total": total, "items": [p["item_id"] for p in per_invoice[doc["doc_id"]]]}
            doc_findings = []
        summaries.append(summary)
        findings += doc_findings
    return {"tolerance": fmt(tol), "lines": lines, "invoice_findings": findings, "invoices": summaries}
