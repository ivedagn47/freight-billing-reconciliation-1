"""Phase 6 deterministic checks: tracing a spec to its contract, behavioural agreement between two
extractions, grounding of agent-written figures, and the worker transcript audit."""

import copy
import json

import pytest

from freight import agreement, audit, contracts, grounding, tracing

from synthetic import ACME_CONTRACT, spec

VOCAB = {"service_level": ["express", "standard"], "special_handling": ["fragile", "residential"]}


@pytest.fixture
def index(tmp_path):
    path = tmp_path / "acme.md"
    path.write_text(ACME_CONTRACT)
    return contracts.index(path)


def mutated(fn):
    data = spec()
    fn(data)
    return data


# ---------------------------------------------------------------- tracing

def test_synthetic_spec_traces_to_its_contract(index):
    assert tracing.check(spec(), index, VOCAB, "acme") == []


TRACING = [
    ("untraced rate", lambda d: d["components"][0]["calc"]["bands"][1].update(rate=1.75), "rate 1.75 does not appear"),
    ("number cited from the wrong clause", lambda d: d["components"][2].update(clauses=["3"]),
     "amount 50 does not appear in its cited clauses ['3']"),
    ("derived percentage", lambda d: d["components"][1]["calc"].update(percent=0.1), "percent 0.1 does not appear"),
    ("band end the contract does not state", lambda d: d["components"][0]["calc"]["bands"][0].update(min=0, min_inclusive=True),
     "bands[0].min 0 does not appear"),
    ("adjustment threshold", lambda d: d["invoice_adjustments"][0]["when"]["consignments_in_billing_month"].update(gt=3),
     "when gt 3 does not appear"),
    ("clause not accounted for", lambda d: d.update(non_pricing=[]), "clause 6 is not cited anywhere"),
    ("clause that does not exist", lambda d: d["gaps"][0].update(clauses=["2", "9"]), "cites clauses that do not exist: ['9']"),
    ("term differs from the header", lambda d: d["term"].update(end="2027-12-31"), "the contract header says 2026-01-01..2026-12-31"),
    ("term not cited to the header", lambda d: d["term"].update(clauses=["1"]), "term must cite"),
    ("agreement reference", lambda d: d.update(agreement_ref="ACME/2"), "agreement_ref is 'ACME/2'"),
    ("contract file", lambda d: d.update(contract_file="other.md"), "contract_file is 'other.md'"),
    ("flag outside the shipment vocabulary", lambda d: d["components"][2]["when"].update(special_handling_includes="residence"),
     "special_handling value 'residence'"),
    ("service outside the shipment vocabulary", lambda d: d["service_levels"].update(allowed=["standard", "same_day"]),
     "service_level value 'same_day'"),
    ("schema problems are reported first", lambda d: d.pop("non_pricing"), "non_pricing"),
]


@pytest.mark.parametrize("label,fn,message", TRACING, ids=[t[0] for t in TRACING])
def test_tracing_problems(index, label, fn, message):
    problems = tracing.check(mutated(fn), index, VOCAB, "acme")
    assert any(message in p for p in problems), problems


def test_tracing_checks_the_assigned_carrier(index):
    assert tracing.check(spec(), index, VOCAB, "other") == ["carrier is 'acme'; this assignment is 'other'"]


# ---------------------------------------------------------------- agreement

def test_names_order_wording_and_equivalent_encodings_agree():
    a, b = spec(), spec()
    b["quantities"][0]["name"] = "billable_weight"
    base = b["components"][0]
    base["name"] = "base"
    base["calc"]["basis"] = base["calc"]["select_by"] = {"quantity": "billable_weight"}
    b["components"][1]["calc"]["of"] = ["base"]
    b["components"] = [b["components"][0], b["components"][2], b["components"][1]]  # residential before fuel
    b["components"][1]["description"] = "worded differently"
    b["components"][1]["charge_codes"].append("freight")  # freight is billed under base anyway
    b["components"][2]["clauses"] = ["2", "3"]
    b["components"][2]["charge_codes"] = ["fuel_surcharge", "freight_incl_fuel"]
    b["invoice_adjustments"][0]["name"] = "volume_discount"
    b["invoice_adjustments"][0]["when"]["consignments_in_billing_month"] = {"gte": 3}  # same as > 2 for counts
    b["gaps"] = []
    result = agreement.compare(a, b, VOCAB)
    assert result["agree"], result["differences"]
    assert result["probes"] > 50


DISAGREE = [
    ("rate", lambda d: d["components"][0]["calc"]["bands"][1].update(rate=1.4), "amount"),
    ("boundary inclusivity", lambda d: d["components"][0]["calc"]["bands"][0].update(max_inclusive=True), "flags"),
    ("minimum weight", lambda d: d["quantities"][0]["args"][1].update(const=20), "amount"),
    ("condition", lambda d: d["components"][2].update(when={"special_handling_includes": "fragile"}), "amount"),
    ("uncontracted charge becomes contracted", lambda d: d["components"][2]["charge_codes"].append("detention"),
     "charge_codes"),
    ("service offered", lambda d: d["service_levels"].update(allowed=["standard", "express"]), "service_offered"),
    ("discount threshold", lambda d: d["invoice_adjustments"][0]["when"]["consignments_in_billing_month"].update(gt=3),
     None),
    ("term", lambda d: d["term"].update(end="2026-11-30"), None),
]


@pytest.mark.parametrize("label,fn,key", DISAGREE, ids=[d[0] for d in DISAGREE])
def test_behavioural_differences_are_found(label, fn, key):
    result = agreement.compare(spec(), mutated(fn), VOCAB)
    assert not result["agree"] and result["differences_total"] >= 1
    if key:
        shipment_diffs = [d for d in result["differences"] if d["aspect"] == "shipment"]
        assert any(key in d["a"] or key in d["b"] for d in shipment_diffs), result["differences"]


def test_probe_grid_samples_thresholds_and_both_sides_of_them():
    values = agreement.grid({agreement.Decimal(100), agreement.Decimal(500)})
    assert {agreement.Decimal(0), agreement.Decimal(100), agreement.Decimal(500), agreement.Decimal(501)} <= set(values)
    assert any(100 < v < 500 for v in values) and any(0 < v < 100 for v in values)


def test_too_many_probes_fail_closed(monkeypatch):
    monkeypatch.setattr(agreement, "MAX_PROBES", 10)
    with pytest.raises(agreement.AgreementError):
        agreement.compare(spec(), spec(), VOCAB)


# ---------------------------------------------------------------- grounding

def test_identifiers_dates_and_references_are_not_figures():
    text = "Consignment FF-1234 on ALPINE-0726 (SHP00123), shipped 2026-07-03, cites §3 and item #2 under AEL/BFC/2025-03."
    assert grounding.figures(text) == []


def test_figures_must_be_copied_from_the_facts():
    facts = {"billed_amount": "1250.50", "delta": "-199.50",
             "evidence": {"shipment": {"billed_weight_kg": 640, "ship_date": "2026-07-03"}}}
    allowed = grounding.fact_numbers(facts, {"clauses": [{"text": "a premium of 15% of base freight"}]})
    faithful = ("Billed ₹1,250.50, which is INR 199.50 more than allowed for 640 kg; a 15% premium applies to "
                "2 charges in July 2026.")
    assert grounding.check(faithful, allowed, "memo") == []
    problems = grounding.check("Overbilled by Rs 200 on 641 kg.", allowed, "memo")
    assert len(problems) == 2 and "amount 'Rs 200'" in problems[0] and "number '641'" in problems[1]


# ---------------------------------------------------------------- audit

def make_worker(tmp_path, uses, cwd, session="sess-1"):
    worker = tmp_path / "run" / "workers" / "extract_rules.extract_fan-0000"
    worker.mkdir(parents=True)
    (worker / "meta.json").write_text(json.dumps({"session_id": session, "cwd": str(cwd)}))
    events = [{"type": "system", "subtype": "init", "session_id": session}]
    events += [{"type": "assistant", "message": {"content": [{"type": "tool_use", "id": f"t{i}", "name": name,
                                                              "input": args}]}} for i, (name, args) in enumerate(uses)]
    (worker / "transcript.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return worker


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "run" / "artefacts" / "branches" / "extract_fan-0000"
    ws.mkdir(parents=True)
    (ws / "rate-spec.json").write_text(json.dumps({"_session_id": "sess-1"}))
    contract = tmp_path / "acme.md"
    contract.write_text(ACME_CONTRACT)
    return ws, contract


def test_audit_accepts_assigned_inputs_and_the_workspace(tmp_path, workspace):
    ws, contract = workspace
    worker = make_worker(tmp_path, [("Read", {"file_path": str(contract)}), ("Glob", {"pattern": "*.json"}),
                                    ("Write", {"file_path": str(ws / "rate-spec.json")}),
                                    ("Edit", {"file_path": "rate-spec.json"})], ws)
    assert audit.output_session_ids(ws) == {"sess-1"}
    assert audit.find_worker(tmp_path / "run" / "workers", "sess-1") == worker
    result = audit.audit(worker, [str(contract)], ws)
    assert result["violations"] == [] and len(result["tool_calls"]) == 4


def test_audit_rejects_anything_outside_the_assignment(tmp_path, workspace):
    ws, contract = workspace
    sibling = tmp_path / "run" / "artefacts" / "branches" / "extract_fan-0001" / "rate-spec.json"
    worker = make_worker(tmp_path, [("Read", {"file_path": str(sibling)}),
                                    ("Grep", {"pattern": "rate", "path": str(tmp_path / "run")}),
                                    ("Glob", {"pattern": f"{tmp_path}/**/*.json"}),
                                    ("Write", {"file_path": str(tmp_path / "notes.md")}),
                                    ("Read", {"file_path": str(contract)}),
                                    ("Bash", {"command": "ls"})], ws)
    violations = audit.audit(worker, [str(contract)], ws)["violations"]
    assert len(violations) == 5 and "Bash" in violations[-1] and "extract_fan-0001" in violations[0]


def test_audit_needs_exactly_one_matching_worker(tmp_path, workspace):
    ws, _ = workspace
    with pytest.raises(audit.AuditError):
        audit.find_worker(tmp_path / "run" / "workers", "sess-unknown")
