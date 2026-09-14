"""Synthetic carrier "acme": invented rules, shipments and documents. No real contract is encoded.

Rules: chargeable_kg = max(billed_weight_kg, 10); freight 2.00/kg below 100 kg and 1.50/kg above 100 kg
up to and including 500 kg (exactly 100 kg is deliberately uncovered); fuel 10% of freight;
residential delivery 50.00 when flagged; 5% invoice discount when more than 2 consignments ship in
the billing month; standard service only; term calendar 2026.
"""

import copy

ACME_SPEC = {
    "spec_version": 1, "carrier": "acme", "contract_file": "acme.md", "agreement_ref": "ACME/1",
    "term": {"start": "2026-01-01", "end": "2026-12-31", "clauses": ["header"]},
    "quantities": [{"name": "chargeable_kg", "op": "max", "args": [{"field": "billed_weight_kg"}, {"const": 10}],
                    "clauses": ["1"]}],
    "components": [
        {"name": "freight", "kind": "freight", "charge_codes": ["freight", "freight_incl_fuel"], "clauses": ["2"],
         "calc": {"op": "banded_rate", "basis": {"quantity": "chargeable_kg"}, "select_by": {"quantity": "chargeable_kg"},
                  "bands": [{"min": None, "min_inclusive": False, "max": 100, "max_inclusive": False, "rate": 2},
                            {"min": 100, "min_inclusive": False, "max": 500, "max_inclusive": True, "rate": 1.5}]}},
        {"name": "fuel", "kind": "surcharge", "charge_codes": ["freight_incl_fuel", "fuel_surcharge"], "clauses": ["3"],
         "calc": {"op": "percent_of", "percent": 10, "of": ["freight"]}},
        {"name": "residential", "kind": "accessorial", "charge_codes": ["residential_delivery"], "clauses": ["4"],
         "when": {"special_handling_includes": "residential"}, "calc": {"op": "flat", "amount": 50}},
    ],
    "service_levels": {"allowed": ["standard"], "clauses": ["2"]},
    "invoice_adjustments": [{"name": "volume", "kind": "discount", "clauses": ["5"],
                             "when": {"consignments_in_billing_month": {"gt": 2}},
                             "calc": {"op": "percent_of_invoice_expected", "percent": 5}}],
    "gaps": [{"description": "exactly 100 kg is in neither band", "clauses": ["2"]}],
}


def spec(**top_level) -> dict:
    data = copy.deepcopy(ACME_SPEC)
    data.update(copy.deepcopy(top_level))
    return data


def shipment(ref: str, kg=50, **fields) -> dict:
    return {"shipment_id": f"S-{ref}", "carrier_consignment_ref": ref, "carrier": "acme", "ship_date": "2026-07-03",
            "billed_weight_kg": kg, "distance_km": 10, "declared_value_inr": 1000, "service_level": "standard",
            "special_handling": [], "delivery_status": "delivered", **fields}


def line(no: int, ref: str, charges: list[tuple[str, str]], total: str | None = None, attributes: dict | None = None):
    total = total or f"{sum(float(a) for _, a in charges):.2f}"
    return {"line_no": no, "consignment_ref": ref, "booking_date": None, "attributes": attributes or {}, "note": None,
            "charges": [{"code": c, "label": c, "amount": a, "derived": False} for c, a in charges],
            "stated_total": total, "source": {"first_line": no, "last_line": no}}


def invoice(doc_id: str, lines: list[dict], period: str = "2026-07", discount: str = "0.00",
            total: str | None = None, failures: list[dict] | None = None) -> dict:
    lines_total = sum(float(l["stated_total"]) for l in lines) - float(discount)
    return {"doc_id": doc_id, "doc_type": "invoice", "carrier": "acme", "format": "synthetic", "billing_period": period,
            "period": None, "issue_date": None, "against_invoice": None,
            "stated": {"total": total or f"{lines_total:.2f}", "line_count": len(lines), "discount": discount},
            "integrity": {"checks": 0, "failures": failures or []}, "lines": lines}


def credit_note(doc_id: str, against: str, lines: list[dict], issue_date: str = "2026-08-01") -> dict:
    return {"doc_id": doc_id, "doc_type": "credit_note", "carrier": "acme", "format": "synthetic", "billing_period": None,
            "period": None, "issue_date": issue_date, "against_invoice": against,
            "stated": {"total": f"{sum(float(l['stated_total']) for l in lines):.2f}", "line_count": None, "discount": None},
            "integrity": {"checks": 0, "failures": []}, "lines": lines}


def codes(priced_line: dict) -> list[str]:
    return [f["code"] for f in priced_line["flags"]]
