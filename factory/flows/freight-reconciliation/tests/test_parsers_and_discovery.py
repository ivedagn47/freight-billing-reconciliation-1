import json
from pathlib import Path

import pytest

from freight import documents, parsers
from freight.parsers.base import ParseError
from freight.vocab import CHARGE_CODES

from conftest import FLOW_DIR, REPO

CARRIERS = FLOW_DIR / "config" / "carriers.yml"

FALCON_INVOICE = """\
FALCON FREIGHT PVT LTD
Servicing BlueFin Commerce under agreement TEST/1
TAX INVOICE TEST-A    Period: 1-14 2030-01
================================================================

1. Consignment FF-1
   Aville to Btown, 1,200 km, 700 kg, express
   Residential delivery: Rs 250.00
   Freight incl. fuel surcharge: Rs 1,000.50
   LINE TOTAL: Rs 1,250.50

2. Consignment FF-2
   Aville to Ctown, 10 km, 5.5 kg, standard
   Mystery fee: Rs 3.00
   Freight incl. fuel surcharge: Rs 10.00
   LINE TOTAL: Rs 14.00

================================================================
INVOICE TOTAL: Rs 1,264.50
Payment due 30 days from invoice date. E&OE.
"""

FALCON_CREDIT = """\
FALCON FREIGHT PVT LTD
CREDIT NOTE TEST-CN    Date: 2030-02-01
Against: TAX INVOICE TEST-A
================================================================

1. Consignment FF-1
   Correction: something was wrong;
   more explanation.
   LINE TOTAL: Rs -12.00

================================================================
CREDIT NOTE TOTAL: Rs -12.00
"""

ALPINE = {
    "carrier": "Alpine Express Logistics", "customer": "X", "invoice_no": "ALP-1", "billing_period": "2030-01",
    "consignment_count": 3,
    "lines": [
        {"sl": 1, "consignment_no": "AE-1", "booking_date": "2030-01-02", "actual_weight_kg": 30,
         "chargeable_weight_kg": 30, "rate_per_kg": 2.5, "handling_fee": 0, "line_amount": 75.0},
        {"sl": 2, "consignment_no": "AE-2", "booking_date": "2030-01-03", "actual_weight_kg": 7.5,
         "chargeable_weight_kg": 7.5, "rate_per_kg": 1.25, "handling_fee": 10, "line_amount": 19.38},
        {"sl": 3, "consignment_no": "AE-3", "booking_date": "2030-01-04", "actual_weight_kg": 10,
         "chargeable_weight_kg": 10, "rate_per_kg": 2, "handling_fee": 0, "line_amount": 21.0},
    ],
    "discount": 5.0, "invoice_total": 110.38,
}

SAGAR_INVOICE = """\
cnote_no,booking_dt,wt_kg,dist_km,freight_rs,chill_prem_rs,total_rs
SG-1,2030-01-02,100,20,640.00,0.00,640.00
SG-2,2030-01-05,50,10,320.00,64.00,384.00
SG-3,2030-01-09,10,10,80.00,0.00,90.00
TOTAL,,,,,,1114.00
"""

SAGAR_CREDIT = """\
credit_note,against_invoice,cnote_no,credit_rs
CN-A,SAGAR-X,SG-1,-10.00
CN-A,SAGAR-X,SG-2,-5.50
CN-B,SAGAR-Y,SG-9,-1.00
TOTAL,,,-16.50
"""


def write(tmp_path: Path, name: str, content) -> Path:
    path = tmp_path / name
    path.write_text(content if isinstance(content, str) else json.dumps(content))
    return path


def failures(doc) -> list[tuple]:
    return [(f["check"], f["line_no"]) for f in doc["integrity"]["failures"]]


# ---------------------------------------------------------------- synthetic formats, exact values

def test_falcon_invoice(tmp_path):
    [doc] = parsers.parse_file(write(tmp_path, "a.txt", FALCON_INVOICE))
    assert (doc["doc_id"], doc["doc_type"], doc["billing_period"], doc["agreement_ref"]) == \
        ("TEST-A", "invoice", "2030-01", "TEST/1")
    assert doc["period"] == {"start": "2030-01-01", "end": "2030-01-14", "label": "1-14 2030-01"}
    first, second = doc["lines"]
    assert first["attributes"] == {"origin": "Aville", "destination": "Btown", "distance_km": 1200, "weight_kg": 700,
                                   "service_level": "express"}
    assert [(c["code"], c["amount"]) for c in first["charges"]] == [("residential_delivery", "250.00"),
                                                                      ("freight_incl_fuel", "1000.50")]
    assert first["stated_total"] == "1250.50" and second["attributes"]["weight_kg"] == 5.5
    assert second["charges"][0] == {"code": "other", "label": "Mystery fee", "amount": "3.00", "derived": False}
    assert doc["stated"]["total"] == "1264.50"
    assert failures(doc) == [("line_total_equals_charges", 2)]


def test_falcon_credit_note(tmp_path):
    [doc] = parsers.parse_file(write(tmp_path, "cn.txt", FALCON_CREDIT))
    assert (doc["doc_type"], doc["against_invoice"], doc["issue_date"], doc["billing_period"]) == \
        ("credit_note", "TEST-A", "2030-02-01", None)
    [line] = doc["lines"]
    assert line["note"] == "Correction: something was wrong; more explanation."
    assert line["charges"] == [{"code": "credit", "label": "credit", "amount": "-12.00", "derived": True}]
    assert failures(doc) == []


@pytest.mark.parametrize("mutation,message", [
    (lambda t: t.replace("TAX INVOICE TEST-A", "TAX INVOIC TEST-A"), "unexpected text"),
    (lambda t: t.replace("INVOICE TOTAL: Rs 1,264.50\n", ""), "no INVOICE TOTAL"),
    (lambda t: t.replace("   LINE TOTAL: Rs 14.00\n", ""), "has no LINE TOTAL"),
    (lambda t: "Stray line\n" + t.replace("FALCON FREIGHT PVT LTD\n", "", 1), "no parser recognises"),
])
def test_falcon_parser_is_strict(tmp_path, mutation, message):
    with pytest.raises(ParseError, match=message):
        parsers.parse_file(write(tmp_path, "bad.txt", mutation(FALCON_INVOICE)))


def test_alpine_invoice(tmp_path):
    [doc] = parsers.parse_file(write(tmp_path, "a.json", ALPINE))
    assert (doc["doc_id"], doc["billing_period"], doc["stated"]) == \
        ("ALP-1", "2030-01", {"total": "110.38", "line_count": 3, "discount": "5.00"})
    second = doc["lines"][1]
    assert [(c["code"], c["amount"], c["derived"]) for c in second["charges"]] == \
        [("freight", "9.38", True), ("handling", "10.00", False)]
    assert second["attributes"] == {"actual_weight_kg": 7.5, "chargeable_weight_kg": 7.5, "rate_per_kg": 1.25}
    # 7.5 x 1.25 = 9.375 -> 9.38 (half-up) + 10 = 19.38 matches; line 3: 10 x 2 = 20 != 21
    assert failures(doc) == [("line_amount_equals_rate_x_weight_plus_handling", 3)]


@pytest.mark.parametrize("change,message", [
    (lambda d: d.pop("invoice_total"), "missing fields"),
    (lambda d: d.update(billing_period="Jan 2030"), "not YYYY-MM"),
    (lambda d: d["lines"][0].pop("line_amount"), "missing fields"),
])
def test_alpine_parser_is_strict(tmp_path, change, message):
    data = json.loads(json.dumps(ALPINE))
    change(data)
    with pytest.raises(ParseError, match=message):
        parsers.parse_file(write(tmp_path, "a.json", data))


def test_sagar_invoice_and_credit_notes(tmp_path):
    [doc] = parsers.parse_file(write(tmp_path, "SAGAR-X.csv", SAGAR_INVOICE))
    assert (doc["doc_id"], doc["id_source"], doc["billing_period"], doc["stated"]["total"]) == \
        ("SAGAR-X", "filename", "2030-01", "1114.00")
    assert [(c["code"], c["amount"]) for c in doc["lines"][1]["charges"]] == [("freight", "320.00"),
                                                                              ("cold_chain_premium", "64.00")]
    assert doc["lines"][0]["attributes"] == {"weight_kg": 100, "distance_km": 20}
    assert failures(doc) == [("line_total_equals_charges", 3)]

    cn_docs = parsers.parse_file(write(tmp_path, "CN.csv", SAGAR_CREDIT))
    assert [(d["doc_id"], d["against_invoice"], len(d["lines"]), d["stated"]["total"]) for d in cn_docs] == \
        [("CN-A", "SAGAR-X", 2, "-15.50"), ("CN-B", "SAGAR-Y", 1, "-1.00")]
    assert all(failures(d) == [] for d in cn_docs)


def test_sagar_mixed_months_and_strictness(tmp_path):
    mixed = SAGAR_INVOICE.replace("2030-01-09", "2030-02-01")
    [doc] = parsers.parse_file(write(tmp_path, "M.csv", mixed))
    assert doc["billing_period"] is None and ("single_billing_month", None) in failures(doc)
    with pytest.raises(ParseError, match="no TOTAL row"):
        parsers.parse_file(write(tmp_path, "N.csv", SAGAR_INVOICE.replace("TOTAL,,,,,,1114.00\n", "")))
    with pytest.raises(ParseError, match="rows after the TOTAL"):
        parsers.parse_file(write(tmp_path, "R.csv", SAGAR_INVOICE + "SG-4,2030-01-10,1,1,8.00,0.00,8.00\n"))
    with pytest.raises(ParseError, match="not YYYY-MM-DD"):
        parsers.parse_file(write(tmp_path, "D.csv", SAGAR_INVOICE.replace("2030-01-02", "02/01/2030")))


def test_unknown_formats_are_refused(tmp_path):
    for name, content in (("x.txt", "SOME OTHER CARRIER\n"), ("x.csv", "a,b\n1,2\n"), ("x.pdf", "%PDF")):
        with pytest.raises(ParseError, match="no parser recognises"):
            parsers.parse_file(write(tmp_path, name, content))


# ---------------------------------------------------------------- discovery and scope

def test_carrier_config_is_validated(tmp_path):
    carriers = documents.load_carriers(CARRIERS)
    assert documents.carrier_for_format(carriers, "falcon-text") == "falcon"
    bad = tmp_path / "c.yml"
    bad.write_text("carriers:\n  x: {name: X, contract: c.md, consignment_prefix: X-, invoice_formats: [pdf]}\n")
    with pytest.raises(documents.DiscoveryError, match="unknown format"):
        documents.load_carriers(bad)
    bad.write_text("carriers:\n  x: {name: X, contract: c.md, consignment_prefix: X-, invoice_formats: [sagar-csv]}\n"
                   "  y: {name: Y, contract: c.md, consignment_prefix: Y-, invoice_formats: [sagar-csv]}\n")
    with pytest.raises(documents.DiscoveryError, match="belongs to both"):
        documents.load_carriers(bad)


def test_scope_rule_on_synthetic_documents(tmp_path):
    carriers = documents.load_carriers(CARRIERS)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    write(inbox, "a.txt", FALCON_INVOICE)                                               # TEST-A, 2030-01
    write(inbox, "b.txt", FALCON_INVOICE.replace("TEST-A", "TEST-B").replace("2030-01", "2030-02"))
    write(inbox, "cn1.txt", FALCON_CREDIT)                                              # against TEST-A
    write(inbox, "cn2.txt", FALCON_CREDIT.replace("TEST-CN", "TEST-CN2").replace("TEST-A", "TEST-B"))
    write(inbox, "cn3.txt", FALCON_CREDIT.replace("TEST-CN", "TEST-CN3").replace("TEST-A", "NOPE"))
    write(inbox, "SAGAR-M.csv", SAGAR_INVOICE.replace("2030-01-09", "2030-02-01").replace("SG-", "SG-"))
    docs = documents.discover(inbox, carriers)
    m = documents.manifest(docs, "2030-01", carriers)
    assert m["in_scope"] == ["TEST-A", "TEST-CN"]
    assert sorted(m["reference"]) == ["TEST-B", "TEST-CN2"]
    assert sorted(m["unresolved"]) == ["SAGAR-M", "TEST-CN3"]
    reasons = {e["doc_id"]: e["reason"] for e in m["documents"]}
    assert reasons["TEST-CN2"] == "credit note correcting TEST-B (billing period 2030-02)"
    assert reasons["TEST-CN3"] == "credit note corrects unknown invoice NOPE"
    assert m["carriers"]["falcon"]["in_scope"] == ["TEST-A", "TEST-CN"]
    assert any(f["check"] == "consignment_prefix" for d in docs if d["doc_id"] == "TEST-A"
               for f in d["integrity"]["failures"]) is False

    write(inbox, "dup.txt", FALCON_INVOICE)
    with pytest.raises(documents.DiscoveryError, match="appears in both"):
        documents.discover(inbox, carriers)
    with pytest.raises(documents.DiscoveryError, match="not YYYY-MM"):
        documents.manifest(docs, "January", carriers)


# ---------------------------------------------------------------- real files: structure only

REAL = {
    "ALPINE-0726": ("invoice", "alpine", "2026-07", None, 40), "ALPINE-0826": ("invoice", "alpine", "2026-08", None, 35),
    "ALPINE-0926": ("invoice", "alpine", "2026-09", None, 45),
    **{f"FALCON-2026-{m}{h}": ("invoice", "falcon", f"2026-{m}", None, 18) for m in ("07", "08", "09") for h in "AB"},
    "FALCON-CN-01": ("credit_note", "falcon", None, "FALCON-2026-07A", 1),
    **{f"SAGAR-{mon}-{n}": ("invoice", "sagar", f"2026-{num}", None, 25)
       for mon, num in (("JUL", "07"), ("AUG", "08"), ("SEP", "09")) for n in "12"},
    "SAGAR-CN-01": ("credit_note", "sagar", None, "SAGAR-AUG-1", 1),
}


def test_every_real_invoice_file_parses_into_the_expected_structure():
    carriers = documents.load_carriers(CARRIERS)
    docs = documents.discover(REPO / "data" / "invoices", carriers)
    got = {d["doc_id"]: (d["doc_type"], d["carrier"], d["billing_period"], d["against_invoice"], len(d["lines"]))
           for d in docs}
    assert got == REAL
    for doc in docs:
        prefix = carriers[doc["carrier"]]["consignment_prefix"]
        for line in doc["lines"]:
            assert line["consignment_ref"].startswith(prefix) and line["stated_total"] is not None
            assert {c["code"] for c in line["charges"]} <= set(CHARGE_CODES) - {"other"}


def test_real_scope_for_july_follows_the_confirmed_rule():
    carriers = documents.load_carriers(CARRIERS)
    m = documents.manifest(documents.discover(REPO / "data" / "invoices", carriers), "2026-07", carriers)
    assert sorted(m["in_scope"]) == ["ALPINE-0726", "FALCON-2026-07A", "FALCON-2026-07B", "FALCON-CN-01",
                                     "SAGAR-JUL-1", "SAGAR-JUL-2"]
    assert "SAGAR-CN-01" in m["reference"] and m["unresolved"] == []
