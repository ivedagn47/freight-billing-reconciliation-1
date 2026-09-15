"""Coverage checks between steps, so nothing is dropped, duplicated or left undecided as volume grows.

    check_scope   after discovery: no document whose scope could not be established, and something in scope
    check_priced  after pricing: every line of every in-scope document priced exactly once, nothing else
                  priced, every in-scope document summarised, every line and finding given a decision or a
                  bounded judgement
"""

import json
from pathlib import Path


class CoverageError(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def check_scope(manifest: dict) -> list[str]:
    problems = [f"{d['doc_id']} ({d['source_file']}) is unresolved: {d['reason']}"
                for d in manifest["documents"] if d["scope"] == "unresolved"]
    if not manifest["in_scope"]:
        problems.append(f"no documents are in scope for {manifest['period']}")
    return problems


def in_scope_line_count(manifest: dict) -> int:
    return sum(d["line_count"] for d in manifest["documents"] if d["scope"] == "in_scope")


def check_priced(manifest: dict, documents_dir: Path, bundle: dict) -> list[str]:
    problems, expected = [], []
    for doc_id in manifest["in_scope"]:
        doc = json.loads((Path(documents_dir) / f"{doc_id}.json").read_text())
        expected += [f"{doc_id}#{line['line_no']}" for line in doc["lines"]]
    if len(expected) != in_scope_line_count(manifest):
        problems.append(f"the in-scope documents hold {len(expected)} lines but the manifest counts "
                        f"{in_scope_line_count(manifest)}")
    priced = bundle["priced"]
    actual = [line["item_id"] for line in priced["lines"]]
    duplicated = sorted({i for i in actual if actual.count(i) > 1})
    missing = [i for i in expected if i not in set(actual)]
    extra = [i for i in actual if i not in set(expected)]
    if duplicated:
        problems.append(f"lines priced more than once: {duplicated[:10]}")
    if missing:
        problems.append(f"{len(missing)} in-scope lines were not priced: {missing[:10]}")
    if extra:
        problems.append(f"{len(extra)} lines were priced that are not in scope: {extra[:10]}")
    summarised = {s["invoice"] for s in priced["invoices"]}
    if set(manifest["in_scope"]) != summarised:
        problems.append(f"invoice summaries cover {sorted(summarised)}; in scope are {sorted(manifest['in_scope'])}")
    decisions = bundle["planned"]["decisions"]
    undecided = [i for i in actual + [f["finding_id"] for f in priced["invoice_findings"]] if i not in decisions]
    if undecided:
        problems.append(f"no policy decision for {undecided[:10]}")
    open_items = {n["item_id"] for n in bundle["planned"]["needs_judgement"]}
    unbounded = [i for i, d in decisions.items() if d["disposition"] is None and i not in open_items]
    if unbounded:
        problems.append(f"undecided lines not offered for judgement: {unbounded[:10]}")
    return problems
