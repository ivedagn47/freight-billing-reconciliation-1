"""Found in the first July dry run: report text and memo ids must not depend on what an extraction worker named
a component or an invoice adjustment, or two runs with identical money would publish different outputs; and a
duplicate billing, whose expected amount comes from the duplicate rule, cites no contract clause."""

from freight import pricing, ratespec

from synthetic import invoice, line, shipment, spec


def renamed() -> dict:
    """The same contract reading with different names and an equivalent count condition."""
    s = spec()
    s["quantities"][0]["name"] = "billable"
    for key in ("basis", "select_by"):
        s["components"][0]["calc"][key] = {"quantity": "billable"}
    s["components"][0]["name"] = "base_haulage"
    s["components"][1]["calc"]["of"] = ["base_haulage"]
    s["invoice_adjustments"][0]["name"] = "monthly_volume_incentive"
    s["invoice_adjustments"][0]["when"]["consignments_in_billing_month"] = {"gte": 3}
    return ratespec.validate(s)


def run(s: dict) -> dict:
    lines = [line(1, "AC-1", [("freight_incl_fuel", "121.00")]), line(2, "AC-2", [("freight", "205.00")]),
             line(3, "AC-9", [("freight", "10.00")]), line(4, "AC-1", [("freight_incl_fuel", "121.00")])]
    ships = [shipment("AC-1", 50), shipment("AC-2", 100), shipment("AC-3", 5)]
    return pricing.price([invoice("ACME-07", lines)], ["ACME-07"], ships, {"acme": s})


def visible(priced: dict):
    return ([(l["item_id"], [f["message"] for f in l["flags"]], l["contract_clauses"]) for l in priced["lines"]],
            [(f["finding_id"], f["description"]) for f in priced["invoice_findings"]],
            [[(a["name"], a["description"]) for a in s["adjustments"]] for s in priced["invoices"]])


def test_messages_and_findings_do_not_depend_on_spec_names():
    original, other = run(ratespec.validate(spec())), run(renamed())
    assert visible(original) == visible(other)
    finding = original["invoice_findings"][0]
    assert finding["finding_id"] == "ACME-07!ADJUSTMENT_UNDETERMINED!discount-5pct-from-3"
    assert finding["description"].startswith("the contract's 5% discount for 3 or more consignments in the billing "
                                              "month applies (3 consignments in 2026-07)")
    gap = next(l for l in original["lines"] if l["item_id"] == "ACME-07#2")
    assert gap["flags"][0]["message"].startswith("the freight charge: no rate band covers 100")


def test_adjustment_terms_state_count_conditions_as_inclusive_bounds():
    base = {"kind": "discount", "calc": {"op": "percent_of_invoice_expected", "percent": 2.50}}
    terms = [pricing.adjustment_terms({**base, "when": {"consignments_in_billing_month": c}})[1]
             for c in ({"gt": 12}, {"gte": 13}, {"lt": 5}, {"lte": 4})]
    assert terms == ["discount-2.5pct-from-13", "discount-2.5pct-from-13", "discount-2.5pct-upto-4",
                     "discount-2.5pct-upto-4"]


def test_a_duplicate_billing_cites_no_clause():
    priced = run(ratespec.validate(spec()))
    first, duplicate = priced["lines"][0], priced["lines"][3]
    assert first["contract_clauses"] and first["expected_amount"] == "110.00"
    assert duplicate["contract_clauses"] == [] and duplicate["expected_amount"] == "0.00"
