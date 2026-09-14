"""Each Phase 6 agent node run in isolation through flowstate, with scripted (fake) workers and the real
prompts, schemas, gates and scripts: a correct output passes its gates, a wrong one is rejected with
feedback the worker can act on, and a retry in the same conversation recovers."""

import json
import shutil
from pathlib import Path

import pytest

import stage_flows
from flowstate import engine
from freight import policy, report

from synthetic import spec

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="agent nodes need tmux")

WAITING = {"worker_running", "script_running", "branches_running"}
OUT = {"capture": {"name": "out", "regex": "at exactly this path: (\\S+)"}}


@pytest.fixture
def runs(tmp_path, monkeypatch):
    prefs = tmp_path / "prefs.yml"
    prefs.write_text("supervision: low\nmax_retries: 2\nstall_after_s: 120\nscript_timeout_s: 120\n")
    monkeypatch.setenv("FLOWSTATE_PREFS", str(prefs))
    return str(tmp_path / "runs")


def advance(run_id: str, runs: str) -> dict:
    while True:
        sit = engine.advance(run_id, runs_dir=runs, max_wait_s=60)
        if sit["situation"] not in WAITING:
            return sit


def fake(tmp_path: Path, name: str, invocations: list[dict]) -> str:
    path = tmp_path / f"fake-{name}.json"
    path.write_text(json.dumps({"model": "fake", "invocations": invocations}))
    return str(path)


def retry_out(filename: str) -> dict:
    return {"capture": {"name": "out", "regex": f"(\\S+/{filename.replace('.', '[.]')})"}}


def event_types(run_id: str, runs: str) -> list[str]:
    return [e["type"] for e in engine.events(run_id, runs_dir=runs)]


# ---------------------------------------------------------------- contract extraction

GOOD_SPEC = {**spec(), "_session_id": "${session_id}"}
UNTRACED_SPEC = {**GOOD_SPEC, "components": [spec()["components"][0], spec()["components"][1],
                                             {**spec()["components"][2], "calc": {"op": "flat", "amount": 55}}]}
BOUNDARY_SPEC = json.loads(json.dumps(GOOD_SPEC))
BOUNDARY_SPEC["components"][0]["calc"]["bands"][0]["max_inclusive"] = True


def write_spec(data: dict, when: str | None = None, unless: str | None = None) -> dict:
    step = {"write": {"path": "${out}", "json": data}}
    if when:
        step["when"] = when
    if unless:
        step["unless"] = unless
    return step


def start_rules(tmp_path: Path, runs: str, run_id: str, extract: str, rerun: str, cache: str = "cache") -> None:
    inputs = stage_flows.acme_inputs(tmp_path / "inputs")
    dot = stage_flows.materialize("stage-rules", tmp_path / "flows")
    engine.init_run(str(dot), [f"carriers_config={inputs['carriers']}", f"data_root={inputs['root']}",
                               f"shipments_file={inputs['shipments']}", "carrier_ids=acme",
                               f"rules_cache_dir={tmp_path / cache}"],
                    run_id=run_id, runs_dir=runs, harness="fake",
                    fake_scripts={"extract_rules": extract, "extract_rules_rerun": rerun})


def test_extraction_copies_pass_their_gates_agree_and_are_cached(tmp_path, runs):
    good = fake(tmp_path, "good", [{"steps": [OUT, write_spec(GOOD_SPEC)], "result": "wrote the rate spec"}])
    start_rules(tmp_path, runs, "rules-1", good, good)
    sit = advance("rules-1", runs)
    assert sit["situation"] == "completed", sit
    final = json.loads(Path(sit["variables"]["rules_final"]).read_text())
    assert final["sources"]["acme"]["source"] == "round 1" and len(set(final["sources"]["acme"]["sessions"])) == 2
    assert len(final["cache_writes"]) == 1
    types = event_types("rules-1", runs)
    assert types.count("gate_passed") == 4 and types.count("worker_completed") == 2

    start_rules(tmp_path, runs, "rules-2", good, good)
    sit = advance("rules-2", runs)
    assert sit["situation"] == "completed", sit
    final = json.loads(Path(sit["variables"]["rules_final"]).read_text())
    assert final["sources"]["acme"]["source"] == "cache"
    assert "worker_completed" not in event_types("rules-2", runs)


def test_untraced_spec_fails_the_gate_and_a_retry_recovers(tmp_path, runs):
    extract = fake(tmp_path, "extract", [
        {"steps": [OUT, write_spec(GOOD_SPEC, unless='"copy": "b"'), write_spec(UNTRACED_SPEC, when='"copy": "b"')]},
        {"steps": [retry_out("rate-spec.json"), write_spec(GOOD_SPEC)], "result": "fixed the amount"}])
    start_rules(tmp_path, runs, "rules-gate", extract, extract)
    sit = advance("rules-gate", runs)
    assert (sit["situation"], sit["node"], sit["branch_id"], sit["gate"]) == \
        ("gate_failed", "extract_rules", "extract_fan-0001", "gates/rate-spec-traced.sh")
    assert "amount 55 does not appear in its cited clauses ['4']" in sit["stderr_tail"]

    engine.retry("rules-gate", "extract_rules", runs_dir=runs, branch=sit["branch_id"])
    sent = (Path(runs) / "rules-gate" / "workers" / "extract_rules.extract_fan-0001" / "invocations" / "001" / "input.md")
    assert "amount 55 does not appear" in sent.read_text()
    assert advance("rules-gate", runs)["situation"] == "completed"


def test_disagreement_runs_a_second_round_which_must_agree(tmp_path, runs):
    extract = fake(tmp_path, "extract", [{"steps": [OUT, write_spec(GOOD_SPEC, unless='"copy": "b"'),
                                                    write_spec(BOUNDARY_SPEC, when='"copy": "b"')]}])
    rerun = fake(tmp_path, "rerun", [{"steps": [OUT, write_spec(GOOD_SPEC)]}])
    start_rules(tmp_path, runs, "rules-round2", extract, rerun)
    sit = advance("rules-round2", runs)
    assert sit["situation"] == "completed", sit
    final = json.loads(Path(sit["variables"]["rules_final"]).read_text())
    assert final["sources"]["acme"] == {**final["sources"]["acme"], "source": "round 2", "matches_round1_copies": ["a"]}
    agreement = json.loads((Path(runs) / "rules-round2" / "artefacts" / "rules" / "agreement-1.json").read_text())
    assert agreement["carriers"]["acme"]["status"] == "disagreed"

    split = fake(tmp_path, "split", [{"steps": [OUT, write_spec(GOOD_SPEC, unless='"copy": "d"'),
                                                write_spec(BOUNDARY_SPEC, when='"copy": "d"')]}])
    start_rules(tmp_path, runs, "rules-split", extract, split, cache="cache-split")  # the first run cached acme
    sit = advance("rules-split", runs)
    assert (sit["situation"], sit["node"]) == ("script_failed", "rules_final")
    assert "acme: disagreed" in sit["stderr_tail"]


# ---------------------------------------------------------------- adjudication

def decisions(first_disposition: str, justification: str) -> dict:
    return {"_session_id": "${session_id}", "batch_id": "adj-001", "decisions": [
        {"item_id": "ACME-07#3", "disposition": first_disposition, "clauses": ["4"], "justification": justification},
        {"item_id": "ACME-07#4", "disposition": "escalate", "clauses": [],
         "justification": "The shipment was booked on a service the agreement does not offer; a person decides."}]}


def test_adjudication_rejects_an_unoffered_disposition_then_merges(tmp_path, runs):
    scenario = stage_flows.acme_priced(tmp_path / "inputs")
    adjudicate = fake(tmp_path, "adjudicate", [
        {"steps": [OUT, {"write": {"path": "${out}", "json": decisions("accept", "Pay it.")}}]},
        {"steps": [retry_out("adjudications.json"), {"write": {"path": "${out}", "json": decisions(
            "dispute", "The detention charge of INR 40.00 is not a charge the contract provides for.")}}]}])
    dot = stage_flows.materialize("stage-adjudicate", tmp_path / "flows")
    engine.init_run(str(dot), [f"priced={scenario['priced']}", f"clauses_dir={scenario['clauses_dir']}"],
                    run_id="adj", runs_dir=runs, harness="fake", fake_scripts={"adjudicate": adjudicate})
    sit = advance("adj", runs)
    assert (sit["situation"], sit["gate"]) == ("gate_failed", "gates/adjudications-grounded.sh")
    assert "not one of the offered options ['dispute', 'escalate']" in sit["stderr_tail"]
    engine.retry("adj", "adjudicate", runs_dir=runs, branch=sit["branch_id"])
    sit = advance("adj", runs)
    assert sit["situation"] == "completed", sit

    merged = json.loads(Path(sit["variables"]["adjudications"]).read_text())["decisions"]
    assert merged["ACME-07#3"]["disposition"] == "dispute"
    bundle = scenario["bundle"]
    rules_doc = policy.load(stage_flows.FLOW_DIR / "config" / "policy.yml")
    rep = report.assemble(bundle["priced"], bundle["planned"], rules_doc, merged)
    report.verify(rep, bundle["priced"], bundle["planned"], stage_flows.REPO / "report.schema.json")


# ---------------------------------------------------------------- memos

MEMO_IDS = ["ACME-07-line-003", "ACME-07-line-004", "ACME-07-line-005", "ACME-07-finding-adjustment-undetermined-volume"]


def drafts(summary_of_first: str) -> dict:
    return {"_session_id": "${session_id}", "batch_id": "memo-001", "memos": [
        {"memo_id": memo_id, "headline": "ACME-07 needs a decision",
         "summary": summary_of_first if n == 0 else "The contract alone does not settle this item.",
         "contract_basis": "Clause 2 sets the freight rates.", "action": "Ask finance to decide."}
        for n, memo_id in enumerate(MEMO_IDS)]}


def test_memo_drafts_with_invented_figures_are_rejected_then_rendered(tmp_path, runs):
    scenario = stage_flows.acme_priced(tmp_path / "inputs")
    write = fake(tmp_path, "memos", [
        {"steps": [OUT, {"write": {"path": "${out}", "json": drafts("The carrier overbilled by INR 123.45.")}}]},
        {"steps": [retry_out("memo-drafts.json"), {"write": {"path": "${out}", "json": drafts(
            "Billed INR 370.00 for a consignment that is still in transit.")}}]}])
    dot = stage_flows.materialize("stage-memos", tmp_path / "flows")
    engine.init_run(str(dot), [f"priced={scenario['priced']}", f"report={scenario['report']}",
                               f"clauses_dir={scenario['clauses_dir']}", f"carriers_config={scenario['carriers']}"],
                    run_id="memos", runs_dir=runs, harness="fake", fake_scripts={"write_memos": write})
    sit = advance("memos", runs)
    assert (sit["situation"], sit["gate"]) == ("gate_failed", "gates/memos-grounded.sh")
    assert "INR 123.45" in sit["stderr_tail"]
    engine.retry("memos", "write_memos", runs_dir=runs, branch=sit["branch_id"])
    sit = advance("memos", runs)
    assert sit["situation"] == "completed", sit

    index = json.loads(Path(sit["variables"]["memos_index"]).read_text())
    memo_dir = Path(index["memos_dir"])
    assert sorted(p.name for p in memo_dir.iterdir()) == sorted(f"{m}.md" for m in MEMO_IDS)
    assert "| Billed | INR 370.00 |" in (memo_dir / "ACME-07-line-003.md").read_text()
