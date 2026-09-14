"""Phase 6 adjudication and memo packets: what agents are given, what their outputs must satisfy, and
how outputs are merged into the report and rendered as memos."""

import copy
import json
from pathlib import Path

import pytest

from freight import adjudication, documents, memos, policy, report

from conftest import FLOW_DIR, REPO
from stage_flows import acme_priced

POLICY = policy.load(FLOW_DIR / "config" / "policy.yml")
MEMO_IDS = ["ACME-07-line-003", "ACME-07-line-004", "ACME-07-line-005",
            "ACME-07-finding-adjustment-undetermined-volume"]


@pytest.fixture
def scenario(tmp_path):
    return acme_priced(tmp_path / "inputs")


def read(path):
    return json.loads(Path(path).read_text())


def good_decisions(packet, session="s1"):
    return {"_session_id": session, "batch_id": packet["batch_id"], "decisions": [
        {"item_id": item["item_id"], "disposition": "escalate", "clauses": ["3"],
         "justification": f"Billed INR {item['billed_amount']}; a person has to decide."} for item in packet["items"]]}


# ---------------------------------------------------------------- adjudication

def test_packets_carry_facts_options_and_contract_text(scenario, tmp_path):
    plan = adjudication.plan(scenario["bundle"], scenario["clauses_dir"], 1, tmp_path / "adj")
    assert [b["item_ids"] for b in plan["batches"]] == [["ACME-07#3"], ["ACME-07#4"]]
    assert plan["batches"][0]["inputs"] == [plan["batches"][0]["packet"]]
    packet = read(plan["batches"][0]["packet"])
    item = packet["items"][0]
    assert (item["options"], item["billed_amount"], item["expected_amount"], item["delta"]) == \
        (["dispute", "escalate"], "370.00", "330.00", "40.00")
    assert {f["code"] for f in item["flags"]} >= {"NOT_DELIVERED", "UNCONTRACTED_CHARGE"}
    assert [c["id"] for c in packet["contracts"]["acme"]["clauses"]] == ["1", "2", "3", "4", "5", "6"]


CHECKS = [
    ("wrong batch", lambda d: d.update(batch_id="adj-009"), "batch_id is 'adj-009'"),
    ("unknown item", lambda d: d["decisions"][0].update(item_id="ACME-07#1"), "not an item in this batch"),
    ("decided twice", lambda d: d["decisions"].append(copy.deepcopy(d["decisions"][0])), "decided more than once"),
    ("missing item", lambda d: d["decisions"].pop(), "no decision for ['ACME-07#4']"),
    ("option not offered", lambda d: d["decisions"][0].update(disposition="accept"), "not one of the offered options"),
    ("clause not in the contract", lambda d: d["decisions"][0].update(clauses=["12"]), "clauses ['12'] do not exist"),
    ("empty justification", lambda d: d["decisions"][0].update(justification="  "), "justification is empty"),
    ("too long", lambda d: d["decisions"][0].update(justification="x" * 601), "limit 600"),
    ("invented amount", lambda d: d["decisions"][0].update(justification="The carrier owes INR 41.00 back."),
     "amount 'INR 41.00' is not an amount in the facts"),
]


@pytest.mark.parametrize("label,fn,message", CHECKS, ids=[c[0] for c in CHECKS])
def test_decision_check_problems(scenario, tmp_path, label, fn, message):
    plan = adjudication.plan(scenario["bundle"], scenario["clauses_dir"], 10, tmp_path / "adj")
    packet = read(plan["batches"][0]["packet"])
    assert adjudication.check(packet, good_decisions(packet)) == []
    decisions = good_decisions(packet)
    fn(decisions)
    problems = adjudication.check(packet, decisions)
    assert any(message in p for p in problems), problems


def test_merge_covers_every_open_item_once_and_feeds_assembly(scenario, tmp_path):
    plan = adjudication.plan(scenario["bundle"], scenario["clauses_dir"], 1, tmp_path / "adj")
    docs = [good_decisions(read(b["packet"]), f"s{n}") for n, b in enumerate(plan["batches"])]
    merged = adjudication.merge(plan["batches"], docs, scenario["bundle"])
    assert sorted(merged["decisions"]) == ["ACME-07#3", "ACME-07#4"]
    assert merged["decisions"]["ACME-07#4"]["session_id"] == "s1"

    priced, planned = scenario["bundle"]["priced"], scenario["bundle"]["planned"]
    rep = report.assemble(priced, planned, POLICY, merged["decisions"])
    report.verify(rep, priced, planned, REPO / "report.schema.json")
    row = next(l for l in rep["lines"] if l["consignment_ref"] == "AC-5")
    assert row["justification"] == "Billed INR 370.00; a person has to decide. (cited: acme.md §3)"

    with pytest.raises(adjudication.AdjudicationError, match="batches but"):
        adjudication.merge(plan["batches"], docs[:1], scenario["bundle"])
    wrong = copy.deepcopy(docs)
    wrong[1]["decisions"][0]["disposition"] = "dispute"
    with pytest.raises(adjudication.AdjudicationError, match="not one of the offered options"):
        adjudication.merge(plan["batches"], wrong, scenario["bundle"])


def test_nothing_open_means_no_batches_and_an_empty_merge(scenario, tmp_path):
    bundle = copy.deepcopy(scenario["bundle"])
    bundle["planned"]["needs_judgement"] = []
    plan = adjudication.plan(bundle, scenario["clauses_dir"], 5, tmp_path / "adj")
    assert plan["batches"] == [] and adjudication.merge([], [], bundle) == {"decisions": {}}


# ---------------------------------------------------------------- memos

def good_drafts(packet, session="m1"):
    return {"_session_id": session, "batch_id": packet["batch_id"], "memos": [
        {"memo_id": m["memo_id"], "headline": f"{m['invoice']}: item needs a decision",
         "summary": "The line cannot be settled from the contract alone.", "contract_basis": "Clause 2 sets the rates.",
         "action": "Ask finance to decide."} for m in packet["memos"]]}


def test_memo_facts_cover_every_non_accept_row(scenario):
    carriers = documents.load_carriers(scenario["carriers"])
    facts = memos.memo_facts(scenario["bundle"], scenario["report_doc"], carriers)
    assert [f["memo_id"] for f in facts] == MEMO_IDS
    assert len(facts) == len(report.memo_items(scenario["report_doc"]))
    assert facts[0]["carrier_name"] == "Acme Test Carriers" and facts[2]["shipment_id"] is None
    out_of_order = copy.deepcopy(scenario["report_doc"])
    out_of_order["lines"].reverse()
    with pytest.raises(memos.MemoError):
        memos.memo_facts(scenario["bundle"], out_of_order, carriers)


MEMO_CHECKS = [
    ("wrong batch", lambda d: d.update(batch_id="memo-007"), "batch_id is 'memo-007'"),
    ("missing memo", lambda d: d["memos"].pop(), "no memo drafted for"),
    ("drafted twice", lambda d: d["memos"].append(copy.deepcopy(d["memos"][0])), "drafted more than once"),
    ("empty field", lambda d: d["memos"][0].update(action=""), "action is empty"),
    ("headline too long", lambda d: d["memos"][0].update(headline="x" * 141), "limit 140"),
    ("invented figure", lambda d: d["memos"][0].update(summary="The carrier overbilled by INR 123.45 on 950 kg."),
     "amount 'INR 123.45'"),
]


@pytest.mark.parametrize("label,fn,message", MEMO_CHECKS, ids=[c[0] for c in MEMO_CHECKS])
def test_memo_check_problems(scenario, tmp_path, label, fn, message):
    plan = memos.plan(scenario["bundle"], scenario["report_doc"], scenario["clauses_dir"],
                      documents.load_carriers(scenario["carriers"]), 10, tmp_path / "memo")
    packet = read(plan["batches"][0]["packet"])
    assert memos.check(packet, good_drafts(packet)) == []
    drafts = good_drafts(packet)
    fn(drafts)
    assert any(message in p for p in memos.check(packet, drafts))


def test_render_writes_one_memo_per_item_with_a_code_generated_table(scenario, tmp_path):
    plan = memos.plan(scenario["bundle"], scenario["report_doc"], scenario["clauses_dir"],
                      documents.load_carriers(scenario["carriers"]), 3, tmp_path / "memo")
    assert [b["memo_ids"] for b in plan["batches"]] == [MEMO_IDS[:3], MEMO_IDS[3:]]
    docs = [good_drafts(read(b["packet"]), f"m{n}") for n, b in enumerate(plan["batches"])]
    index = memos.render(plan["batches"], docs, tmp_path / "memos")
    assert sorted(p.name for p in (tmp_path / "memos").iterdir()) == sorted(f"{m}.md" for m in MEMO_IDS)
    line3 = (tmp_path / "memos" / "ACME-07-line-003.md").read_text()
    assert "| Billed | INR 370.00 |" in line3 and "| Difference (billed − contract) | INR 40.00 |" in line3
    assert "worker session m0" in line3
    assert "| Contract amount | not determined |" in (tmp_path / "memos" / "ACME-07-line-005.md").read_text()
    assert [m["disposition"] for m in index["memos"]] == ["escalate"] * 4

    (tmp_path / "memos2").mkdir()
    (tmp_path / "memos2" / "old.md").write_text("stale")
    with pytest.raises(memos.MemoError, match="not memos of this run"):
        memos.render(plan["batches"], docs, tmp_path / "memos2")
    bad = copy.deepcopy(docs)
    bad[1]["memos"][0]["summary"] = "About INR 5.55."
    with pytest.raises(memos.MemoError, match="INR 5.55"):
        memos.render(plan["batches"], bad, tmp_path / "memos3")
