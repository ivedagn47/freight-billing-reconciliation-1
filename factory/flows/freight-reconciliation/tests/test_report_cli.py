import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from freight import policy, pricing, ratespec, report

from conftest import FLOW_DIR, REPO
from synthetic import credit_note, invoice, line, shipment, spec

POLICY = policy.load(FLOW_DIR / "config" / "policy.yml")
SCHEMA = REPO / "report.schema.json"


@pytest.fixture
def scenario():
    inv = invoice("ACME-07", [
        line(1, "AC-1", [("freight_incl_fuel", "121.00")]),                          # offset by the credit note
        line(2, "AC-3", [("freight_incl_fuel", "22.00"), ("residential_delivery", "50.00")]),   # exact
        line(3, "AC-1", [("freight_incl_fuel", "121.00")]),                          # duplicate: dispute 121
        line(4, "AC-5", [("freight_incl_fuel", "330.00"), ("detention", "40.00")]),  # judgement: dispute/escalate
        line(5, "AC-9", [("freight", "10.00")]),                                     # no shipment: escalate
    ])
    cn = credit_note("CN-1", "ACME-07", [line(1, "AC-1", [("credit", "-11.00")])])
    ships = [shipment("AC-1", 50), shipment("AC-3", 5, special_handling=["residential"]),
             shipment("AC-5", 200, delivery_status="in_transit")]
    priced = pricing.price([inv, cn], ["ACME-07", "CN-1"], ships, {"acme": ratespec.validate(spec())})
    planned = policy.apply(priced, POLICY)
    adjudications = {"ACME-07#4": {"disposition": "dispute", "justification": "Detention is not in the contract.",
                                   "clauses": ["2"]}}
    return priced, planned, adjudications


def test_assembled_report_is_valid_and_counts_each_rupee_once(scenario):
    priced, planned, adjudications = scenario
    assert [n["item_id"] for n in planned["needs_judgement"]] == ["ACME-07#4"]
    rep = report.assemble(priced, planned, POLICY, adjudications)
    report.verify(rep, priced, planned, SCHEMA, in_scope_lines=6)
    assert [l["disposition"] for l in rep["lines"]] == ["accept", "accept", "dispute", "dispute", "escalate", "accept"]
    assert rep["summary"] == {"total_billed": 683.0, "total_expected": None, "total_in_dispute": 161.0, "line_count": 6,
                              "counts_by_disposition": {"accept": 3, "dispute": 2, "escalate": 1}}
    assert rep["lines"][3]["justification"] == "Detention is not in the contract. (cited: acme.md §2)"
    assert rep["lines"][3]["contract_clause"] == "acme.md §1, §2, §3"  # what the expected amount is computed from
    assert rep["lines"][0]["contract_clause"] == "acme.md §1, §2, §3"
    assert "credit note line CN-1#1" in rep["lines"][0]["justification"]
    assert [f["disposition"] for f in rep["invoice_findings"]] == ["escalate"]          # adjustment undetermined
    assert [(t["invoice"], t["expected_total"]) for t in rep["invoice_totals"]] == [("ACME-07", None), ("CN-1", -11.0)]
    assert [(m["kind"], m.get("consignment_ref"), m["disposition"]) for m in report.memo_items(rep)] == [
        ("line", "AC-1", "dispute"), ("line", "AC-5", "dispute"), ("line", "AC-9", "escalate"), ("finding", None, "escalate")]


@pytest.mark.parametrize("adjudications,message", [
    ({}, "missing adjudication"),
    ({"ACME-07#4": {"disposition": "accept", "justification": "x"}}, "not one of"),
    ({"ACME-07#4": {"disposition": "dispute", "justification": "  "}}, "no justification"),
    ({"ACME-07#4": {"disposition": "dispute", "justification": "x"}, "ACME-07#2": {"disposition": "dispute",
                                                                                   "justification": "x"}}, "already decided"),
])
def test_adjudications_are_bounded_by_policy(scenario, adjudications, message):
    priced, planned, _ = scenario
    with pytest.raises(report.AssemblyError) as exc:
        report.assemble(priced, planned, POLICY, adjudications)
    assert any(message in p for p in exc.value.problems)


@pytest.mark.parametrize("tamper,message", [
    (lambda r: r["lines"][2].update(delta=1.0), "delta is not billed - expected"),
    (lambda r: r["lines"].pop(), "report has 5 lines"),
    (lambda r: r["lines"][0].update(disposition="dispute"), "offset by a credit note but disputed"),
    (lambda r: r["lines"][1].update(disposition="escalate"), "not allowed by policy"),
    (lambda r: r["summary"].update(total_in_dispute=40.0), "total_in_dispute"),
    (lambda r: r["summary"]["counts_by_disposition"].update(accept=4), "counts_by_disposition"),
    (lambda r: r["lines"][4].update(expected_amount=10.0), "null together"),
    (lambda r: r["invoice_totals"][0].update(billed_total=1.0), "invoice total for ACME-07"),
    (lambda r: r["lines"][0].pop("justification"), "schema"),
])
def test_verify_catches_inconsistent_reports(scenario, tamper, message):
    priced, planned, adjudications = scenario
    rep = report.assemble(priced, planned, POLICY, adjudications)
    broken = copy.deepcopy(rep)
    tamper(broken)
    with pytest.raises(report.AssemblyError) as exc:
        report.verify(broken, priced, planned, SCHEMA)
    assert any(message in p for p in exc.value.problems), exc.value.problems


# ---------------------------------------------------------------- CLI end to end on synthetic files

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
   Aville to Ctown, 10 km, 5 kg, standard
   Mystery fee: Rs 3.00
   Freight incl. fuel surcharge: Rs 10.00
   LINE TOTAL: Rs 14.00

================================================================
INVOICE TOTAL: Rs 1,264.50
"""

CONTRACT = "# Test\n\n**Agreement ref:** TEST/1\n**Term:** 1 January 2030 – 31 December 2030\n\n## Charges\n\n" \
           "1. Freight is ₹1.00 per km.\n2. Residential delivery is ₹250.00.\n"

FALCON_SPEC = {
    "spec_version": 1, "carrier": "falcon", "contract_file": "test.md", "agreement_ref": "TEST/1",
    "term": {"start": "2030-01-01", "end": "2030-12-31", "clauses": ["header"]}, "quantities": [],
    "components": [
        {"name": "freight", "kind": "freight", "charge_codes": ["freight", "freight_incl_fuel"], "clauses": ["1"],
         "calc": {"op": "per_unit", "basis": {"field": "distance_km"}, "rate": 1}},
        {"name": "residential", "kind": "accessorial", "charge_codes": ["residential_delivery"], "clauses": ["2"],
         "when": {"special_handling_includes": "residential"}, "calc": {"op": "flat", "amount": 250}}],
    "service_levels": {"allowed": ["standard", "express"], "clauses": ["1"]}, "invoice_adjustments": [], "gaps": [],
    "non_pricing": [], "unrepresentable": [],
}


def cli(tmp_path, *args):
    env = {**os.environ, "PYTHONPATH": str(FLOW_DIR)}
    proc = subprocess.run([sys.executable, "-m", "freight", *args], capture_output=True, text=True, cwd=tmp_path, env=env)
    return proc.returncode, json.loads(proc.stdout.strip().splitlines()[-1])


def test_cli_end_to_end(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a.txt").write_text(FALCON_INVOICE)
    (tmp_path / "test.md").write_text(CONTRACT)
    (tmp_path / "shipments.json").write_text(json.dumps([
        {**shipment("FF-1", 700, distance_km=1200, service_level="express", special_handling=["residential"],
                    ship_date="2030-01-05"), "carrier": "falcon"},
        {**shipment("FF-2", 5, distance_km=10, ship_date="2030-01-06"), "carrier": "falcon"}]))
    good, bad = tmp_path / "spec.json", tmp_path / "bad-spec.json"
    good.write_text(json.dumps(FALCON_SPEC))
    bad.write_text(json.dumps({**FALCON_SPEC, "components": [{**FALCON_SPEC["components"][0], "clauses": ["9"]}]}))

    code, out = cli(tmp_path, "discover", "--invoices", "inbox", "--carriers", str(FLOW_DIR / "config" / "carriers.yml"),
                    "--period", "2030-01", "--out", "work/manifest.json", "--documents", "work/documents")
    assert code == 0 and out["in_scope"] == ["TEST-A"]
    code, out = cli(tmp_path, "clauses", "--contract", "test.md", "--out", "work/clauses.json")
    assert code == 0 and out["clauses"] == ["1", "2"]
    assert cli(tmp_path, "check-spec", "--spec", "spec.json", "--clauses", "work/clauses.json")[0] == 0
    code, out = cli(tmp_path, "check-spec", "--spec", "bad-spec.json", "--clauses", "work/clauses.json")
    assert code == 1 and "do not exist" in out["error"]["message"]

    code, out = cli(tmp_path, "price", "--manifest", "work/manifest.json", "--documents", "work/documents",
                    "--shipments", "shipments.json", "--spec", "falcon=spec.json",
                    "--policy", str(FLOW_DIR / "config" / "policy.yml"), "--out", "work/priced.json")
    assert code == 0 and (out["lines"], out["needs_judgement"]) == (2, 0)
    code, out = cli(tmp_path, "assemble", "--priced", "work/priced.json", "--policy", str(FLOW_DIR / "config" / "policy.yml"),
                    "--schema", str(SCHEMA), "--out", "work/report.json")
    assert code == 0 and out["counts"] == {"accept": 1, "dispute": 0, "escalate": 1}
    rep = json.loads((tmp_path / "work" / "report.json").read_text())
    first, second = rep["lines"]
    assert (first["expected_amount"], first["delta"], first["disposition"]) == (1450.0, -199.5, "accept")
    assert second["disposition"] == "escalate" and "could not be classified" in second["justification"]

    code, out = cli(tmp_path, "price", "--manifest", "work/manifest.json", "--documents", "work/documents",
                    "--shipments", "shipments.json", "--spec", "falcon=bad-spec.json",
                    "--policy", str(FLOW_DIR / "config" / "policy.yml"), "--out", "work/x.json")
    assert code == 0  # clause existence is check-spec's job; the spec itself is structurally valid
    code, out = cli(tmp_path, "price", "--manifest", "work/manifest.json", "--documents", "work/documents",
                    "--shipments", "shipments.json", "--spec", "falcon=missing.json",
                    "--policy", str(FLOW_DIR / "config" / "policy.yml"), "--out", "work/x.json")
    assert code == 1 and out["error"]["type"] == "FileNotFoundError"
