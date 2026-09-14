"""freight: command-line entry points for the flow's script nodes and gates.

    python -m freight discover --invoices DIR --carriers YML --period YYYY-MM --out FILE --documents DIR
    python -m freight clauses --contract MD --out FILE
    python -m freight check-spec --spec FILE [--clauses FILE] [--vocabulary FILE] [--carrier ID] [--item JSON]
    python -m freight price --manifest FILE --documents DIR --shipments FILE --spec CARRIER=FILE ... --policy YML --out FILE
    python -m freight assemble --priced FILE --policy YML --schema FILE --out FILE [--adjudications FILE]

    python -m freight rules-plan --carriers YML --root DIR --shipments FILE --out-dir DIR --out FILE
                                 [--manifest FILE | --carrier-ids a,b] --cache-dir DIR --use-cache true|false
    python -m freight rules-agree --plan FILE --specs-json JSON --out-dir DIR --out FILE
    python -m freight rules-final --plan FILE --agreement FILE --specs-json JSON --out-dir DIR --out FILE [--run-id ID]
    python -m freight audit-worker --run-dir DIR --workspace DIR --item JSON
    python -m freight adjudication-plan --priced FILE --clauses-dir DIR --batch-size N --out-dir DIR --out FILE
    python -m freight check-adjudications --item JSON --decisions FILE
    python -m freight merge-adjudications --batches FILE --decisions-json JSON --priced FILE --out FILE
    python -m freight memo-plan --priced FILE --report FILE --clauses-dir DIR --carriers YML --batch-size N --out-dir DIR --out FILE
    python -m freight check-memos --item JSON --drafts FILE
    python -m freight render-memos --batches FILE --drafts-json JSON --out-dir DIR --out FILE

Each command prints a one-line JSON summary on stdout. Errors print {"error": ...} on stdout, a short
readable list of problems on stderr (at most 10 lines, which is what a gate's feedback shows a worker),
and exit 1. --item and --*-json take JSON text (a fan-out item or a merged list variable).
"""

import argparse
import json
import sys
from pathlib import Path

from . import (adjudication, agreement, audit, contracts, documents, memos, policy, pricing, ratespec, report, rules,
               tracing)

MAX_STDERR_PROBLEMS = 8


def _write(path: str, data) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2, sort_keys=False) + "\n")


def _read(path: str):
    return json.loads(Path(path).read_text())


def _json_text(text: str, what: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{what} is not valid JSON: {exc}") from exc


def _json_list(text: str, what: str) -> list:
    value = _json_text(text, what)
    if not isinstance(value, list):
        raise ValueError(f"{what} must be a JSON list")
    return value


# ---------------------------------------------------------------- Phase 5 commands

def cmd_discover(args) -> dict:
    carriers = documents.load_carriers(Path(args.carriers))
    docs = documents.discover(Path(args.invoices), carriers)
    for doc in docs:
        _write(str(Path(args.documents) / f"{doc['doc_id']}.json"), doc)
    manifest = documents.manifest(docs, args.period, carriers)
    _write(args.out, manifest)
    return {"documents": len(docs), "in_scope": manifest["in_scope"], "unresolved": manifest["unresolved"]}


def cmd_clauses(args) -> dict:
    index = contracts.index(Path(args.contract))
    _write(args.out, index)
    return {"file": index["file"], "clauses": [c["id"] for c in index["clauses"]]}


def cmd_check_spec(args) -> dict:
    item = _json_text(args.item, "--item") if args.item else {}
    clauses_path = args.clauses or item.get("clause_index")
    carrier = args.carrier or item.get("carrier")
    vocab = _read(args.vocabulary) if args.vocabulary else item.get("vocabulary")
    spec = _read(args.spec)
    if not clauses_path:
        ratespec.validate(spec)
        return {"carrier": spec["carrier"], "components": [c["name"] for c in spec["components"]], "traced": False}
    problems = tracing.check(spec, _read(clauses_path), vocab, carrier)
    if problems:
        raise ratespec.SpecError(problems)
    return {"carrier": spec["carrier"], "components": [c["name"] for c in spec["components"]], "traced": True}


def cmd_price(args) -> dict:
    manifest = _read(args.manifest)
    docs = [_read(str(p)) for p in sorted(Path(args.documents).glob("*.json"))]
    specs = {}
    for pair in args.spec:
        carrier, _, path = pair.partition("=")
        specs[carrier] = ratespec.load(Path(path))
    rules_doc = policy.load(Path(args.policy))
    priced = pricing.price(docs, manifest["in_scope"], _read(args.shipments), specs, str(rules_doc["tolerance_inr"]))
    planned = policy.apply(priced, rules_doc)
    _write(args.out, {"priced": priced, "planned": planned})
    return {"lines": len(priced["lines"]), "findings": len(priced["invoice_findings"]),
            "needs_judgement": len(planned["needs_judgement"])}


def cmd_assemble(args) -> dict:
    bundle = _read(args.priced)
    rules_doc = policy.load(Path(args.policy))
    adjudications = _read(args.adjudications) if args.adjudications else {}
    if isinstance(adjudications.get("decisions"), dict):
        adjudications = adjudications["decisions"]
    assembled = report.assemble(bundle["priced"], bundle["planned"], rules_doc, adjudications)
    report.verify(assembled, bundle["priced"], bundle["planned"], Path(args.schema))
    _write(args.out, assembled)
    return {"lines": assembled["summary"]["line_count"], "counts": assembled["summary"]["counts_by_disposition"],
            "memo_items": len(report.memo_items(assembled))}


# ---------------------------------------------------------------- contract extraction

def cmd_rules_plan(args) -> dict:
    carriers = documents.load_carriers(Path(args.carriers))
    if args.manifest:
        ids = rules.carriers_in_scope(_read(args.manifest))
    else:
        ids = [c.strip() for c in (args.carrier_ids or "").split(",") if c.strip()]
    use_cache = args.use_cache == "true"
    plan_doc = rules.plan(carriers, ids, Path(args.root), _read(args.shipments), Path(args.out_dir),
                          Path(args.cache_dir) if args.cache_dir else None, use_cache)
    _write(args.out, plan_doc)
    return {"carriers": {c: e["cache"] for c, e in plan_doc["carriers"].items()},
            "extraction_items": len(plan_doc["extraction_items"])}


def _blocked(record: dict, blocking: dict) -> rules.RulesError:
    problems = []
    for carrier, status in blocking.items():
        detail = record.get(carrier) or {}
        reasons = detail.get("problems") or [u["description"] for u in detail.get("unrepresentable") or []]
        problems.append(f"{carrier}: {status}" + (f" ({'; '.join(reasons[:3])})" if reasons else ""))
    return rules.RulesError(problems)


def cmd_rules_agree(args) -> dict:
    record, blocking = rules.agree(_read(args.plan), _json_list(args.specs_json, "--specs-json"), Path(args.out_dir))
    _write(args.out, record)
    if blocking:
        raise _blocked(record["carriers"], blocking)
    return {"carriers": {c: r["status"] for c, r in record["carriers"].items()}, "rerun_items": len(record["rerun_items"])}


def cmd_rules_final(args) -> dict:
    record, blocking = rules.final(_read(args.plan), _read(args.agreement), _json_list(args.specs_json, "--specs-json"),
                                   Path(args.out_dir), args.run_id)
    _write(args.out, record)
    if blocking:
        details = {**_read(args.agreement)["carriers"], **record["round2"]}
        raise _blocked(details, blocking)
    return {"specs": sorted(record["specs"]), "sources": {c: s["source"] for c, s in record["sources"].items()},
            "cache_writes": len(record["cache_writes"])}


def cmd_audit_worker(args) -> dict:
    item = _json_text(args.item, "--item")
    sessions = audit.output_session_ids(Path(args.workspace))
    if len(sessions) != 1:
        raise audit.AuditError([f"expected the outputs in {args.workspace} to carry one _session_id, found {sorted(sessions)}"])
    worker = audit.find_worker(Path(args.run_dir) / "workers", sessions.pop())
    result = audit.audit(worker, item["inputs"], Path(args.workspace))
    if result["violations"]:
        raise audit.AuditError(result["violations"] + [
            "a worker that read or wrote outside its inputs cannot be repaired by retrying; respawn it"])
    return {"worker": result["worker"], "tool_calls": len(result["tool_calls"]), "violations": 0}


# ---------------------------------------------------------------- adjudication and memos

def cmd_adjudication_plan(args) -> dict:
    doc = adjudication.plan(_read(args.priced), Path(args.clauses_dir), args.batch_size, Path(args.out_dir))
    _write(args.out, doc)
    return {"items": doc["items"], "batches": len(doc["batches"])}


def cmd_check_adjudications(args) -> dict:
    item = _json_text(args.item, "--item")
    problems = adjudication.check(_read(item["packet"]), _read(args.decisions))
    if problems:
        raise adjudication.AdjudicationError(problems)
    return {"batch_id": item["batch_id"], "decisions": len(item["item_ids"])}


def cmd_merge_adjudications(args) -> dict:
    batches = _read(args.batches)["batches"]
    docs = [_read(p) for p in _json_list(args.decisions_json, "--decisions-json")]
    merged = adjudication.merge(batches, docs, _read(args.priced))
    _write(args.out, merged)
    return {"decisions": len(merged["decisions"])}


def cmd_memo_plan(args) -> dict:
    doc = memos.plan(_read(args.priced), _read(args.report), Path(args.clauses_dir),
                     documents.load_carriers(Path(args.carriers)), args.batch_size, Path(args.out_dir))
    _write(args.out, doc)
    return {"memos": doc["memos"], "batches": len(doc["batches"])}


def cmd_check_memos(args) -> dict:
    item = _json_text(args.item, "--item")
    problems = memos.check(_read(item["packet"]), _read(args.drafts))
    if problems:
        raise memos.MemoError(problems)
    return {"batch_id": item["batch_id"], "memos": len(item["memo_ids"])}


def cmd_render_memos(args) -> dict:
    batches = _read(args.batches)["batches"]
    docs = [_read(p) for p in _json_list(args.drafts_json, "--drafts-json")]
    index = memos.render(batches, docs, Path(args.out_dir))
    _write(args.out, index)
    return {"memos": len(index["memos"]), "memos_dir": index["memos_dir"]}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="freight", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def command(name: str, required: tuple[str, ...] = (), optional: tuple[str, ...] = ()) -> argparse.ArgumentParser:
        p = sub.add_parser(name)
        for flag in required:
            p.add_argument(flag, required=True)
        for flag in optional:
            p.add_argument(flag)
        return p

    command("discover", ("--invoices", "--carriers", "--period", "--out", "--documents"))
    command("clauses", ("--contract", "--out"))
    command("check-spec", ("--spec",), ("--clauses", "--vocabulary", "--carrier", "--item"))
    p = command("price", ("--manifest", "--documents", "--shipments", "--policy", "--out"))
    p.add_argument("--spec", action="append", required=True, help="CARRIER=PATH")
    command("assemble", ("--priced", "--policy", "--schema", "--out"), ("--adjudications",))
    p = command("rules-plan", ("--carriers", "--root", "--shipments", "--out-dir", "--out"),
                ("--manifest", "--carrier-ids", "--cache-dir"))
    p.add_argument("--use-cache", choices=("true", "false"), default="true")
    command("rules-agree", ("--plan", "--specs-json", "--out-dir", "--out"))
    command("rules-final", ("--plan", "--agreement", "--specs-json", "--out-dir", "--out"), ("--run-id",))
    command("audit-worker", ("--run-dir", "--workspace", "--item"))
    p = command("adjudication-plan", ("--priced", "--clauses-dir", "--out-dir", "--out"))
    p.add_argument("--batch-size", type=int, required=True)
    command("check-adjudications", ("--item", "--decisions"))
    command("merge-adjudications", ("--batches", "--decisions-json", "--priced", "--out"))
    p = command("memo-plan", ("--priced", "--report", "--clauses-dir", "--carriers", "--out-dir", "--out"))
    p.add_argument("--batch-size", type=int, required=True)
    command("check-memos", ("--item", "--drafts"))
    command("render-memos", ("--batches", "--drafts-json", "--out-dir", "--out"))
    return parser


COMMANDS = {"discover": cmd_discover, "clauses": cmd_clauses, "check-spec": cmd_check_spec, "price": cmd_price,
            "assemble": cmd_assemble, "rules-plan": cmd_rules_plan, "rules-agree": cmd_rules_agree,
            "rules-final": cmd_rules_final, "audit-worker": cmd_audit_worker,
            "adjudication-plan": cmd_adjudication_plan, "check-adjudications": cmd_check_adjudications,
            "merge-adjudications": cmd_merge_adjudications, "memo-plan": cmd_memo_plan, "check-memos": cmd_check_memos,
            "render-memos": cmd_render_memos}
ERRORS = (documents.DiscoveryError, documents.ParseError, contracts.ContractError, ratespec.SpecError,
          pricing.PricingError, policy.PolicyError, report.AssemblyError, rules.RulesError, agreement.AgreementError,
          audit.AuditError, adjudication.AdjudicationError, memos.MemoError, OSError, ValueError, KeyError)


def _explain(command: str, exc: Exception, problems: list[str] | None) -> str:
    if not problems:
        return f"freight {command}: {type(exc).__name__}: {exc}\n"
    lines = [f"freight {command}: {len(problems)} problem(s):"]
    lines += [f"- {p}" for p in problems[:MAX_STDERR_PROBLEMS]]
    if len(problems) > MAX_STDERR_PROBLEMS:
        lines.append(f"- ... and {len(problems) - MAX_STDERR_PROBLEMS} more (full list on stdout)")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = COMMANDS[args.command](args)
    except ERRORS as exc:
        problems = getattr(exc, "problems", None)
        print(json.dumps({"error": {"type": type(exc).__name__, "message": str(exc),
                                    **({"problems": problems} if problems else {})}}))
        sys.stderr.write(_explain(args.command, exc, problems))
        return 1
    print(json.dumps({"ok": True, **result}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
