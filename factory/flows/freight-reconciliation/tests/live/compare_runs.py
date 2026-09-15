"""Compare two completed freight-reconciliation runs: what is identical and what varied, and why.

    orchestrator/.venv/bin/python factory/flows/freight-reconciliation/tests/live/compare_runs.py RUN_DIR_A RUN_DIR_B

Reports, as JSON:
  - specs: per carrier, whether the two runs' adopted rate specs price identically (agreement.compare);
  - amounts: every report line's billed / expected / delta, every finding's impact and every invoice total,
    compared exactly;
  - dispositions: every line whose disposition differs, and whether each run decided it by policy or by
    adjudication (only adjudicated lines can legitimately vary when the specs agree);
  - memos: the set of memo files each run published.
"""

import json
import sys
from pathlib import Path

FLOW_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(FLOW_DIR))

from freight import agreement  # noqa: E402


def load(run: Path, rel: str):
    return json.loads((run / "artefacts" / rel).read_text())


def compare(a: Path, b: Path) -> dict:
    plan = load(a, "rules/plan.json")
    specs = {}
    for carrier, entry in plan["carriers"].items():
        spec_a, spec_b = load(a, f"rules/specs/{carrier}.json"), load(b, f"rules/specs/{carrier}.json")
        result = agreement.compare(spec_a, spec_b, plan["vocabulary"], entry["invoice_charge_codes"])
        specs[carrier] = {"price_identically": result["agree"], "probes": result["probes"],
                          "differences": result["differences"][:5]}

    report_a, report_b = load(a, "report/reconciliation-report.json"), load(b, "report/reconciliation-report.json")
    priced = load(a, "pricing/priced.json")["priced"]["lines"]
    adjudicated_a = set(load(a, "adjudication/merged.json")["decisions"])
    adjudicated_b = set(load(b, "adjudication/merged.json")["decisions"])
    amount_diffs, disposition_diffs = [], []
    if len(report_a["lines"]) != len(report_b["lines"]):
        amount_diffs.append({"lines": [len(report_a["lines"]), len(report_b["lines"])]})
    for n, (la, lb) in enumerate(zip(report_a["lines"], report_b["lines"])):
        item = priced[n]["item_id"] if n < len(priced) else n
        fields = ("invoice", "consignment_ref", "shipment_id", "billed_amount", "expected_amount", "delta",
                  "contract_clause")
        changed = {f: [la.get(f), lb.get(f)] for f in fields if la.get(f) != lb.get(f)}
        if changed:
            amount_diffs.append({"item": item, **changed})
        if la["disposition"] != lb["disposition"]:
            disposition_diffs.append({"item": item, "a": la["disposition"], "b": lb["disposition"],
                                      "decided_by": ["adjudication" if item in adjudicated_a else "policy",
                                                     "adjudication" if item in adjudicated_b else "policy"]})
    findings = [[fa.get("amount_impact"), fb.get("amount_impact"), fa["disposition"], fb["disposition"]]
                for fa, fb in zip(report_a["invoice_findings"], report_b["invoice_findings"])
                if (fa.get("amount_impact"), fa["disposition"]) != (fb.get("amount_impact"), fb["disposition"])]
    memos_a = sorted(p.name for p in (a / "artefacts" / "memos").glob("*.md"))
    memos_b = sorted(p.name for p in (b / "artefacts" / "memos").glob("*.md"))
    adjudicated_same = sum(1 for i in adjudicated_a & adjudicated_b
                           if i not in {d["item"] for d in disposition_diffs})
    return {
        "runs": [str(a), str(b)],
        "specs": specs,
        "amounts_identical": not amount_diffs and report_a["invoice_totals"] == report_b["invoice_totals"]
        and not findings,
        "amount_differences": amount_diffs[:20],
        "finding_differences": findings,
        "invoice_totals_identical": report_a["invoice_totals"] == report_b["invoice_totals"],
        "summaries": [report_a["summary"], report_b["summary"]],
        "adjudicated_items": [len(adjudicated_a), len(adjudicated_b)],
        "adjudicated_items_decided_the_same": adjudicated_same,
        "disposition_differences": disposition_diffs,
        "memos_identical_set": memos_a == memos_b,
        "memo_counts": [len(memos_a), len(memos_b)],
    }


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    print(json.dumps(compare(Path(sys.argv[1]), Path(sys.argv[2])), indent=2))
