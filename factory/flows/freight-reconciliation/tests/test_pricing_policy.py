import copy

import pytest
import yaml

from freight import policy, pricing, ratespec

from conftest import FLOW_DIR
from synthetic import codes, credit_note, invoice, line, shipment, spec

POLICY = policy.load(FLOW_DIR / "config" / "policy.yml")


def run(docs, in_scope, shipments, acme=None):
    return pricing.price(docs, in_scope, shipments, {"acme": ratespec.validate(acme or spec())})


def one(p, item):
    return next(l for l in p["lines"] if l["item_id"] == item)


# ---------------------------------------------------------------- spec evaluation

@pytest.mark.parametrize("kg,expected", [
    (50, "110.00"),        # 50 x 2.00 + 10%
    (5, "22.00"),          # minimum chargeable 10 kg
    (99.99, "219.98"),     # 199.98 + 19.998 = 219.978 -> half-up
    (100.5, "165.83"),     # 150.75 + 15.075 = 165.825 -> half-up
    (500, "825.00"),       # inclusive top of the second band
])
def test_evaluate_prices_from_the_shipment(kg, expected):
    result = pricing.evaluate(spec(), shipment("AC-1", kg))
    assert str(result["amount"]) == expected and result["flags"] == []


def test_evaluate_conditions_and_components():
    s = spec()
    s["components"][2]["when"] = {"all": [{"special_handling_includes": "residential"},
                                          {"not": {"service_level": "express"}}]}
    ratespec.validate(s)
    applied = pricing.evaluate(s, shipment("AC-1", 50, special_handling=["residential"]))
    assert str(applied["amount"]) == "160.00" and applied["clauses"] == ["2", "3", "4", "1"]
    skipped = pricing.evaluate(s, shipment("AC-1", 50, special_handling=["residential"], service_level="express"))
    assert str(skipped["amount"]) == "110.00" and [r["applied"] for r in skipped["components"]] == [True, True, False]


@pytest.mark.parametrize("kg,code", [(100, "CONTRACT_GAP"), (600, "OUTSIDE_RATE_CARD")])
def test_values_outside_the_bands_are_never_snapped(kg, code):
    result = pricing.evaluate(spec(), shipment("AC-1", kg))
    assert result["amount"] is None and [f["code"] for f in result["flags"]] == [code]


def test_missing_shipment_data_is_reported():
    result = pricing.evaluate(spec(), shipment("AC-1", None))
    assert result["amount"] is None and [f["code"] for f in result["flags"]] == ["SHIPMENT_DATA_MISSING"]


# ---------------------------------------------------------------- invoice lines

def test_line_flags_and_amounts():
    inv = invoice("ACME-07", [
        line(1, "AC-1", [("freight_incl_fuel", "121.00")], attributes={"weight_kg": 60}),
        line(2, "AC-2", [("freight_incl_fuel", "110.00"), ("residential_delivery", "50.00")]),
        line(3, "AC-3", [("freight_incl_fuel", "330.00"), ("detention", "40.00")]),
        line(4, "AC-4", [("freight_incl_fuel", "88.00")]),
        line(5, "AC-5", [("mystery", "5.00")]),
        line(6, "AC-9", [("freight", "10.00")]),
        line(7, "AC-6", [("freight", "22.00")]),
        line(8, "AC-7", [("freight", "22.00")]),
        line(9, "AC-8", [("freight", "22.00")]),
    ], failures=[{"check": "line_total_equals_charges", "line_no": 4, "message": "charges do not add up",
                  "expected": "80.00", "actual": "88.00"}])
    inv["lines"][4]["charges"][0]["code"] = "other"
    ships = [shipment("AC-1", 50), shipment("AC-2", 50), shipment("AC-3", 200, delivery_status="in_transit"),
             shipment("AC-4", 40, service_level="express"), shipment("AC-5", 50),
             shipment("AC-6", 5, carrier="other"), shipment("AC-7", 5, ship_date="2027-01-05"),
             shipment("AC-8", 5), shipment("AC-8", 5)]
    p = run([inv], ["ACME-07"], ships)
    rows = {l["line_no"]: l for l in p["lines"]}
    assert (rows[1]["expected_amount"], rows[1]["delta"], codes(rows[1])) == ("110.00", "11.00", ["ATTRIBUTE_MISMATCH"])
    assert (rows[2]["expected_amount"], rows[2]["delta"], codes(rows[2])) == ("110.00", "50.00", ["CHARGE_NOT_APPLICABLE"])
    assert (rows[3]["expected_amount"], codes(rows[3])) == ("330.00", ["NOT_DELIVERED", "UNCONTRACTED_CHARGE"])
    assert codes(rows[4]) == ["SERVICE_NOT_OFFERED", "COMPONENT_ARITHMETIC"]
    assert codes(rows[5]) == ["UNRECOGNIZED_CHARGE", "UNDERBILLED"]   # billed 5.00 against 110.00
    assert (rows[6]["expected_amount"], rows[6]["shipment_id"], codes(rows[6])) == (None, None, ["NO_SHIPMENT_MATCH"])
    assert (rows[7]["expected_amount"], codes(rows[7])) == (None, ["CARRIER_MISMATCH"])
    assert (rows[8]["expected_amount"], codes(rows[8])) == (None, ["OUTSIDE_TERM"])
    assert codes(rows[9]) == ["SHIPMENT_AMBIGUOUS"]
    assert rows[1]["evidence"]["shipment"]["billed_weight_kg"] == 50
    assert [c["matched_components"] for c in rows[2]["billed_charges"]] == [["freight", "fuel"], ["residential"]]


def test_duplicate_billing_across_periods_and_within_an_invoice():
    june = invoice("ACME-06", [line(1, "AC-1", [("freight_incl_fuel", "110.00")])], period="2026-06")
    july = invoice("ACME-07", [line(1, "AC-1", [("freight_incl_fuel", "110.00")]),
                               line(2, "AC-2", [("freight_incl_fuel", "110.00")]),
                               line(3, "AC-2", [("freight_incl_fuel", "110.00")])])
    p = run([july, june], ["ACME-07"], [shipment("AC-1"), shipment("AC-2")])
    assert [l["item_id"] for l in p["lines"]] == ["ACME-07#1", "ACME-07#2", "ACME-07#3"]   # reference docs not priced
    first, second, third = p["lines"]
    assert codes(first) == ["DUPLICATE_BILLING"] and first["flags"][0]["details"]["first_billed_on"] == "ACME-06#1"
    assert (first["expected_amount"], first["delta"]) == ("0.00", "110.00")
    assert codes(second) == [] and codes(third) == ["DUPLICATE_BILLING"]


@pytest.mark.parametrize("credits,residuals,original_offsets", [
    (["-11.00"], ["0.00"], 1),              # exact correction
    (["-6.00"], ["5.00"], 1),               # partial: 5.00 still owed on the credit line
    (["-15.00"], ["-4.00"], 1),             # over-credit
    (["-6.00", "-5.00"], ["5.00", "0.00"], 2),   # two credit notes, earlier credit counted
])
def test_credit_notes_follow_the_confirmed_rule(credits, residuals, original_offsets):
    inv = invoice("ACME-07", [line(1, "AC-1", [("freight_incl_fuel", "121.00")])])
    notes = [credit_note(f"CN-{i}", "ACME-07", [line(1, "AC-1", [("credit", c)])], issue_date=f"2026-08-0{i + 1}")
             for i, c in enumerate(credits)]
    p = run([inv, *notes], ["ACME-07", *(n["doc_id"] for n in notes)], [shipment("AC-1")])
    original = one(p, "ACME-07#1")
    assert (original["expected_amount"], original["delta"]) == ("110.00", "11.00")
    assert codes(original).count("CREDIT_NOTE_OFFSET") == original_offsets
    credit_lines = [l for l in p["lines"] if l["doc_type"] == "credit_note"]
    assert [l["delta"] for l in credit_lines] == residuals
    assert credit_lines[0]["expected_amount"] == "-11.00" and credit_lines[0]["contract_clauses"] == original["contract_clauses"]
    if residuals[0].startswith("-"):
        assert "UNDERBILLED" in codes(credit_lines[0])


def test_credit_notes_that_cannot_be_resolved():
    inv = invoice("ACME-07", [line(1, "AC-1", [("freight_incl_fuel", "220.00")])])   # 100 kg: contract gap
    cn = credit_note("CN-1", "ACME-07", [line(1, "AC-1", [("credit", "-5.00")]), line(2, "AC-7", [("credit", "-1.00")])])
    p = run([inv, cn], ["ACME-07", "CN-1"], [shipment("AC-1", 100)])
    assert codes(one(p, "CN-1#1")) == ["CREDIT_NOTE_UNDETERMINED", "CREDIT_NOTE"]
    assert (one(p, "CN-1#2")["expected_amount"], codes(one(p, "CN-1#2"))) == (None, ["CREDIT_NOTE_UNMATCHED"])


def test_invoice_adjustment_and_total_findings():
    lines = [line(1, "AC-1", [("freight_incl_fuel", "110.00")]), line(2, "AC-2", [("freight_incl_fuel", "72.00"),]),
             line(3, "AC-3", [("freight_incl_fuel", "330.00")])]
    ships = [shipment("AC-1", 50), shipment("AC-2", 5, special_handling=["residential"]), shipment("AC-3", 200)]
    lines[1]["charges"] = [{"code": "freight_incl_fuel", "label": "f", "amount": "22.00", "derived": False},
                           {"code": "residential_delivery", "label": "r", "amount": "50.00", "derived": False}]

    missing = run([invoice("ACME-07", copy.deepcopy(lines))], ["ACME-07"], ships)
    [finding] = missing["invoice_findings"]
    assert (finding["code"], finding["direction"], finding["amount_impact"]) == ("ADJUSTMENT_MISMATCH", "over", "25.60")
    [summary] = missing["invoices"]
    assert (summary["expected_lines_total"], summary["expected_total"], summary["adjustments"][0]["expected_amount"]) == \
        ("512.00", "486.40", "25.60")

    applied = run([invoice("ACME-07", copy.deepcopy(lines), discount="25.60")], ["ACME-07"], ships)
    assert applied["invoice_findings"] == []

    not_due = run([invoice("ACME-07", copy.deepcopy(lines), discount="5.00")], ["ACME-07"], ships[:2])
    [under] = not_due["invoice_findings"]
    assert (under["direction"], under["amount_impact"]) == ("under", "-5.00")

    failure = {"check": "document_total_equals_lines", "line_no": None, "message": "m", "expected": "512.00",
               "actual": "520.00"}
    mismatch = run([invoice("ACME-07", copy.deepcopy(lines), discount="25.60", total="520.00", failures=[failure])],
                   ["ACME-07"], ships)
    assert [(f["code"], f["amount_impact"]) for f in mismatch["invoice_findings"]] == [("INVOICE_TOTAL_MISMATCH", "8.00")]


def test_pricing_refuses_incomplete_inputs():
    inv = invoice("ACME-07", [line(1, "AC-1", [("freight", "1.00")])])
    with pytest.raises(pricing.PricingError, match="no rate spec"):
        pricing.price([inv], ["ACME-07"], [], {})
    with pytest.raises(pricing.PricingError, match="not parsed"):
        pricing.price([inv], ["ACME-99"], [], {"acme": spec()})
    with pytest.raises(pricing.PricingError, match="name a different carrier"):
        pricing.price([inv], ["ACME-07"], [], {"acme": spec(carrier="other")})


# ---------------------------------------------------------------- policy

def decide(delta, flags=(), expected="100.00"):
    item = {"item_id": "X#1", "expected_amount": None if expected is None else expected,
            "delta": None if expected is None else delta, "flags": [{"code": c} for c in flags]}
    return policy.decide_line(item, POLICY)


@pytest.mark.parametrize("delta,flags,expected,outcome", [
    ("0.00", [], "100.00", "accept"),
    ("0.01", [], "100.00", "accept"),
    ("0.02", [], "100.00", "dispute"),
    ("-5.00", ["UNDERBILLED"], "100.00", "accept"),
    (None, [], None, "escalate"),
    ("50.00", ["NO_SHIPMENT_MATCH"], "100.00", "escalate"),
    ("11.00", ["CREDIT_NOTE_OFFSET"], "100.00", "accept"),
    ("110.00", ["DUPLICATE_BILLING"], "0.00", "dispute"),
    ("0.00", ["UNCONTRACTED_CHARGE"], "100.00", "dispute"),
    ("40.00", ["NOT_DELIVERED"], "100.00", ["dispute", "escalate"]),
    ("0.00", ["SERVICE_NOT_OFFERED", "ATTRIBUTE_MISMATCH"], "100.00", ["accept", "escalate"]),
    ("11.00", ["CREDIT_NOTE_OFFSET", "DUPLICATE_BILLING"], "100.00", "accept"),
    ("11.00", ["CONTRACT_GAP", "NOT_DELIVERED"], "100.00", "escalate"),
])
def test_policy_combination(delta, flags, expected, outcome):
    decision = decide(delta, flags, expected)
    assert (decision["options"] if isinstance(outcome, list) else decision["disposition"]) == outcome


def test_policy_fails_closed(tmp_path):
    with pytest.raises(policy.PolicyError, match="has no policy"):
        decide("0.00", ["SOMETHING_NEW"])
    data = yaml.safe_load((FLOW_DIR / "config" / "policy.yml").read_text())
    for change, message in ((lambda d: d["line_flags"].pop("NOT_DELIVERED"), "missing"),
                            (lambda d: d["line_flags"].update(NOT_DELIVERED="ignore"), "unknown effects"),
                            (lambda d: d["invoice_findings"].update(ADJUSTMENT_MISMATCH={"over": "pay"}), "directions"),
                            (lambda d: d.update(tolerance_inr="a cent"), "tolerance")):
        broken = copy.deepcopy(data)
        change(broken)
        path = tmp_path / "p.yml"
        path.write_text(yaml.safe_dump(broken))
        with pytest.raises(policy.PolicyError, match=message):
            policy.load(path)
    assert policy.decide_finding({"finding_id": "f", "code": "ADJUSTMENT_UNDETERMINED", "direction": "undetermined"},
                                 POLICY) == "escalate"
    with pytest.raises(policy.PolicyError, match="direction"):
        policy.decide_finding({"finding_id": "f", "code": "ADJUSTMENT_MISMATCH", "direction": "undetermined"}, POLICY)
