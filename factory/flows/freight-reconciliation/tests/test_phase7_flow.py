"""The freight-reconciliation flow end to end through flowstate, with scripted (fake) workers and synthetic
data for the invented `acme` carrier: discovery, extraction with agreement and cache, pricing, adjudication,
assembly, memos and publication, every gate on the path, and the digest covering the freight package."""

import json
import shutil
from pathlib import Path

import jsonschema
import pytest

import stage_flows
from flowstate import engine
from flowstate.loader import Issues, _digest_include, load_flow

from synthetic import spec

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="agent nodes need tmux")

FLOW = stage_flows.FLOW_DIR / "freight-reconciliation.dot"
OUT = {"capture": {"name": "out", "regex": "at exactly this path: (\\S+)"}}
WAITING = {"worker_running", "script_running", "branches_running"}

# An acme invoice in Alpine's JSON layout (the synthetic carrier config maps alpine-json to acme). Designed
# outcomes: 1 exact; 2 carries an uncontracted handling fee; 3 is not delivered and 4 used a service the
# contract does not offer (both left open by policy); 5 has no shipment record.
ACME_INVOICE = {
    "carrier": "Alpine Express Logistics", "customer": "BlueFin Commerce", "invoice_no": "ACME-0726",
    "billing_period": "2026-07", "consignment_count": 5, "discount": 0, "invoice_total": 684.0,
    "lines": [
        {"sl": 1, "consignment_no": "AC-1", "booking_date": "2026-07-03", "handling_fee": 0, "line_amount": 110.0},
        {"sl": 2, "consignment_no": "AC-3", "booking_date": "2026-07-03", "handling_fee": 30, "line_amount": 102.0},
        {"sl": 3, "consignment_no": "AC-5", "booking_date": "2026-07-03", "handling_fee": 0, "line_amount": 330.0},
        {"sl": 4, "consignment_no": "AC-7", "booking_date": "2026-07-03", "handling_fee": 0, "line_amount": 132.0},
        {"sl": 5, "consignment_no": "AC-9", "booking_date": "2026-07-03", "handling_fee": 0, "line_amount": 10.0},
    ],
}
MEMO_IDS = ["ACME-0726-line-002", "ACME-0726-line-003", "ACME-0726-line-005",
            "ACME-0726-finding-adjustment-undetermined-discount-5pct-from-3"]


@pytest.fixture
def runs(tmp_path, monkeypatch):
    prefs = tmp_path / "prefs.yml"
    prefs.write_text("supervision: low\nmax_retries: 2\nstall_after_s: 120\nscript_timeout_s: 120\n")
    monkeypatch.setenv("FLOWSTATE_PREFS", str(prefs))
    return str(tmp_path / "runs")


def fake(tmp_path: Path, name: str, document: dict) -> str:
    path = tmp_path / f"fake-{name}.json"
    path.write_text(json.dumps({"model": "fake", "invocations": [
        {"steps": [OUT, {"write": {"path": "${out}", "json": document}}], "result": f"wrote {name}"}]}))
    return str(path)


def advance(run_id: str, runs: str) -> dict:
    while True:
        sit = engine.advance(run_id, runs_dir=runs, max_wait_s=60)
        if sit["situation"] not in WAITING:
            return sit


def start(tmp_path: Path, runs: str, run_id: str) -> Path:
    inputs = stage_flows.acme_inputs(tmp_path / "inputs")
    invoices = tmp_path / "inputs" / "invoices"
    invoices.mkdir(exist_ok=True)
    (invoices / "ACME-0726.json").write_text(json.dumps(ACME_INVOICE, indent=2))
    workers = {
        "extract_rules": fake(tmp_path, "extract", {**spec(), "_session_id": "${session_id}"}),
        "extract_rules_rerun": fake(tmp_path, "extract", {**spec(), "_session_id": "${session_id}"}),
        "adjudicate": fake(tmp_path, "adjudicate", {"_session_id": "${session_id}", "batch_id": "adj-001", "decisions": [
            {"item_id": "ACME-0726#3", "disposition": "escalate", "clauses": [],
             "justification": "The consignment is still in transit, so a person should decide before it is paid."},
            {"item_id": "ACME-0726#4", "disposition": "accept", "clauses": ["2"],
             "justification": "The billed amount matches the contract rate; the service level does not change it."}]}),
        "write_memos": fake(tmp_path, "memos", {"_session_id": "${session_id}", "batch_id": "memo-001", "memos": [
            {"memo_id": m, "headline": "ACME-0726 needs attention", "summary": "The line could not be accepted as billed.",
             "contract_basis": "Clause 2 sets the freight rates.", "action": "Follow up as the disposition says."}
            for m in MEMO_IDS]}),
    }
    publish_dir = tmp_path / "published"
    engine.init_run(str(FLOW), [
        "period=2026-07", f"invoices_dir={invoices}", f"shipments_file={inputs['shipments']}",
        f"carriers_config={inputs['carriers']}", f"data_root={inputs['root']}",
        f"report_schema={stage_flows.REPO / 'report.schema.json'}", f"rules_cache_dir={tmp_path / 'cache'}",
        f"publish_dir={publish_dir}"], run_id=run_id, runs_dir=runs, harness="fake", fake_scripts=workers)
    return publish_dir


def test_the_flow_reconciles_and_publishes_end_to_end(tmp_path, runs):
    publish_dir = start(tmp_path, runs, "e2e")
    sit = advance("e2e", runs)
    assert sit["situation"] == "completed", sit

    publication = json.loads(Path(sit["variables"]["publication"]).read_text())
    published = json.loads((publish_dir / "reconciliation-report.json").read_text())
    jsonschema.Draft7Validator(json.loads((stage_flows.REPO / "report.schema.json").read_text())).validate(published)
    assert [(l["consignment_ref"], l["disposition"]) for l in published["lines"]] == [
        ("AC-1", "accept"), ("AC-3", "dispute"), ("AC-5", "escalate"), ("AC-7", "accept"), ("AC-9", "escalate")]
    assert published["summary"]["counts_by_disposition"] == {"accept": 2, "dispute": 1, "escalate": 2}
    assert [f["disposition"] for f in published["invoice_findings"]] == ["escalate"]
    assert publication["summary"] == published["summary"]
    assert sorted(p.stem for p in (publish_dir / "memos").iterdir()) == sorted(MEMO_IDS)

    gates = [e["gate"] for e in engine.events("e2e", runs_dir=runs) if e["type"] == "gate_passed"]
    assert sorted(set(gates)) == sorted(["gates/scope-resolved.sh", "gates/rate-spec-traced.sh",
                                         "gates/worker-inputs-only.sh", "gates/priced-covers-scope.sh",
                                         "gates/adjudications-grounded.sh", "gates/memos-grounded.sh"])
    assert len(gates) == 10  # 2 extraction copies x 2, adjudication x 2, memos x 2, scope, pricing

    final = json.loads((Path(runs) / "e2e" / "artefacts" / "rules" / "final.json").read_text())
    assert final["sources"]["acme"]["source"] == "round 1" and len(final["cache_writes"]) == 1


def test_the_digest_covers_the_freight_package_and_config():
    yml = FLOW.with_suffix(".flow.yml")
    included = {p.relative_to(stage_flows.FLOW_DIR).as_posix() for p in _digest_include(yml, FLOW.parent, Issues())}
    assert {"freight/pricing.py", "freight/parsers/falcon_text.py", "config/policy.yml", "config/carriers.yml",
            "scripts/_paths.sh"} <= included
    assert not any("tests/" in p for p in included)
    assert load_flow(FLOW, yml).digest
