"""Phase 7 wiring: coverage checks between steps, publication, the price CLI's spec map, and the flow's copy
of the report schema."""

import copy
import json
from pathlib import Path

import pytest

from freight import adjudication, cli, coverage, documents, memos, publish, report

from conftest import FLOW_DIR, REPO
from stage_flows import acme_priced
from test_phase6_packets import good_drafts


def manifest_for(bundle: dict, unresolved: bool = False) -> dict:
    invoices = sorted({line["invoice"] for line in bundle["priced"]["lines"]})
    docs = [{"doc_id": i, "source_file": f"{i}.json", "scope": "in_scope", "reason": "test",
             "line_count": sum(1 for l in bundle["priced"]["lines"] if l["invoice"] == i)} for i in invoices]
    if unresolved:
        docs.append({"doc_id": "X-1", "source_file": "x.csv", "scope": "unresolved", "reason": "period unknown",
                     "line_count": 1})
    return {"period": "2026-07", "documents": docs, "in_scope": invoices,
            "reference": [], "unresolved": ["X-1"] if unresolved else []}


def documents_dir(tmp_path: Path, bundle: dict) -> Path:
    out = tmp_path / "documents"
    out.mkdir()
    for invoice in {line["invoice"] for line in bundle["priced"]["lines"]}:
        lines = [{"line_no": l["line_no"]} for l in bundle["priced"]["lines"] if l["invoice"] == invoice]
        (out / f"{invoice}.json").write_text(json.dumps({"doc_id": invoice, "lines": lines}))
    return out


@pytest.fixture
def scenario(tmp_path):
    return acme_priced(tmp_path / "inputs")


def test_scope_must_be_resolved_and_non_empty(scenario):
    assert coverage.check_scope(manifest_for(scenario["bundle"])) == []
    assert "X-1 (x.csv) is unresolved: period unknown" in coverage.check_scope(manifest_for(scenario["bundle"], True))
    empty = {**manifest_for(scenario["bundle"]), "in_scope": []}
    assert coverage.check_scope(empty) == ["no documents are in scope for 2026-07"]


def test_every_in_scope_line_is_priced_once_and_decided(scenario, tmp_path):
    bundle, manifest = scenario["bundle"], manifest_for(scenario["bundle"])
    docs = documents_dir(tmp_path, bundle)
    assert coverage.check_priced(manifest, docs, bundle) == []

    dropped = copy.deepcopy(bundle)
    dropped["priced"]["lines"].pop(0)
    assert any("not priced" in p for p in coverage.check_priced(manifest, docs, dropped))
    doubled = copy.deepcopy(bundle)
    doubled["priced"]["lines"].append(doubled["priced"]["lines"][0])
    assert any("more than once" in p for p in coverage.check_priced(manifest, docs, doubled))
    undecided = copy.deepcopy(bundle)
    del undecided["planned"]["decisions"]["ACME-07#1"]
    assert any("no policy decision" in p for p in coverage.check_priced(manifest, docs, undecided))
    unbounded = copy.deepcopy(bundle)
    unbounded["planned"]["needs_judgement"] = []
    assert any("not offered for judgement" in p for p in coverage.check_priced(manifest, docs, unbounded))


def rendered_memos(scenario, tmp_path) -> dict:
    carriers = documents.load_carriers(scenario["carriers"])
    plan = memos.plan(scenario["bundle"], scenario["report_doc"], scenario["clauses_dir"], carriers, 8, tmp_path / "memo")
    docs = [good_drafts(json.loads(Path(b["packet"]).read_text())) for b in plan["batches"]]
    return memos.render(plan["batches"], docs, tmp_path / "run-memos")


def test_publication_reverifies_and_copies_report_and_memos(scenario, tmp_path):
    index = rendered_memos(scenario, tmp_path)
    carriers = documents.load_carriers(scenario["carriers"])
    manifest = manifest_for(scenario["bundle"])
    dest = tmp_path / "published"
    (dest / "memos").mkdir(parents=True)
    (dest / "memos" / "stale-from-an-earlier-run.md").write_text("old")
    record = publish.publish(scenario["report"], scenario["bundle"], manifest, index, carriers,
                             REPO / "report.schema.json", dest)
    assert json.loads((dest / "reconciliation-report.json").read_text()) == scenario["report_doc"]
    assert sorted(p.stem for p in (dest / "memos").iterdir()) == sorted(record["memos"]) and len(record["memos"]) == 4
    assert not list(dest.glob(".memos-publishing-*")) and not list(dest.glob(".report-publishing-*"))

    (dest / "memos" / "notes.txt").write_text("someone's file")
    with pytest.raises(publish.PublishError, match="refusing to replace"):
        publish.publish(scenario["report"], scenario["bundle"], manifest, index, carriers, REPO / "report.schema.json", dest)
    (dest / "memos" / "notes.txt").unlink()

    short = {**index, "memos": index["memos"][1:]}
    with pytest.raises(publish.PublishError, match="memo index lists 3"):
        publish.publish(scenario["report"], scenario["bundle"], manifest, short, carriers, REPO / "report.schema.json", dest)
    tampered = tmp_path / "tampered.json"
    doc = copy.deepcopy(scenario["report_doc"])
    doc["summary"]["total_in_dispute"] += 1
    tampered.write_text(json.dumps(doc))
    with pytest.raises(report.AssemblyError):
        publish.publish(tampered, scenario["bundle"], manifest, index, carriers, REPO / "report.schema.json", dest)
    extra = {**manifest, "documents": manifest["documents"] + [{**manifest["documents"][0], "doc_id": "Z", "line_count": 2}]}
    with pytest.raises(report.AssemblyError, match="in-scope documents have"):
        publish.publish(scenario["report"], scenario["bundle"], extra, index, carriers, REPO / "report.schema.json", dest)


def test_price_accepts_the_adopted_spec_map(tmp_path, capsys):
    from test_report_cli import FALCON_INVOICE, FALCON_SPEC
    from synthetic import shipment
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a.txt").write_text(FALCON_INVOICE)
    (tmp_path / "shipments.json").write_text(json.dumps([
        {**shipment("FF-1", 700, distance_km=1200, service_level="express", special_handling=["residential"],
                    ship_date="2030-01-05"), "carrier": "falcon"},
        {**shipment("FF-2", 5, distance_km=10, ship_date="2030-01-06"), "carrier": "falcon"}]))
    (tmp_path / "spec.json").write_text(json.dumps(FALCON_SPEC))
    assert cli.main(["discover", "--invoices", str(inbox), "--carriers", str(FLOW_DIR / "config" / "carriers.yml"),
                     "--period", "2030-01", "--out", str(tmp_path / "manifest.json"),
                     "--documents", str(tmp_path / "docs")]) == 0
    args = ["price", "--manifest", str(tmp_path / "manifest.json"), "--documents", str(tmp_path / "docs"),
            "--shipments", str(tmp_path / "shipments.json"), "--policy", str(FLOW_DIR / "config" / "policy.yml"),
            "--out", str(tmp_path / "priced.json")]
    assert cli.main(args + ["--specs-json", json.dumps({"falcon": str(tmp_path / "spec.json")})]) == 0
    assert cli.main(args) == 1 and "give --spec" in capsys.readouterr().err
    assert cli.main(["check-priced", "--manifest", str(tmp_path / "manifest.json"), "--documents", str(tmp_path / "docs"),
                     "--priced", str(tmp_path / "priced.json")]) == 0


def test_the_flow_validates_its_report_against_the_fixed_contract():
    assert json.loads((FLOW_DIR / "definitions" / "reconciliation-report.json").read_text()) == \
        json.loads((REPO / "report.schema.json").read_text())
