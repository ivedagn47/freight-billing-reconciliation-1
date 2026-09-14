"""freight: command-line entry points for the flow's script nodes.

    python -m freight discover --invoices DIR --carriers YML --period YYYY-MM --out FILE --documents DIR
    python -m freight clauses --contract MD --out FILE
    python -m freight check-spec --spec FILE [--clauses FILE]
    python -m freight price --manifest FILE --documents DIR --shipments FILE --spec CARRIER=FILE ... --policy YML --out FILE
    python -m freight assemble --priced FILE --policy YML --schema FILE --out FILE [--adjudications FILE]

Each command writes JSON and prints a one-line JSON summary. Errors print {"error": ...} and exit 1.
"""

import argparse
import json
import sys
from pathlib import Path

from . import contracts, documents, policy, pricing, ratespec, report


def _write(path: str, data) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2, sort_keys=False) + "\n")


def _read(path: str):
    return json.loads(Path(path).read_text())


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
    spec = ratespec.load(Path(args.spec))
    result = {"carrier": spec["carrier"], "components": [c["name"] for c in spec["components"]]}
    if args.clauses:
        known = {c["id"] for c in _read(args.clauses)["clauses"]} | {"header"}
        unknown = {k: [c for c in v if c not in known] for k, v in ratespec.cited_clauses(spec).items()}
        unknown = {k: v for k, v in unknown.items() if v}
        if unknown:
            raise ratespec.SpecError([f"{k} cites clauses that do not exist: {v}" for k, v in unknown.items()])
    return result


def cmd_price(args) -> dict:
    manifest = _read(args.manifest)
    docs = [_read(str(p)) for p in sorted(Path(args.documents).glob("*.json"))]
    specs = {}
    for pair in args.spec:
        carrier, _, path = pair.partition("=")
        specs[carrier] = ratespec.load(Path(path))
    rules = policy.load(Path(args.policy))
    priced = pricing.price(docs, manifest["in_scope"], _read(args.shipments), specs, str(rules["tolerance_inr"]))
    planned = policy.apply(priced, rules)
    _write(args.out, {"priced": priced, "planned": planned})
    return {"lines": len(priced["lines"]), "findings": len(priced["invoice_findings"]),
            "needs_judgement": len(planned["needs_judgement"])}


def cmd_assemble(args) -> dict:
    bundle = _read(args.priced)
    rules = policy.load(Path(args.policy))
    adjudications = _read(args.adjudications) if args.adjudications else {}
    assembled = report.assemble(bundle["priced"], bundle["planned"], rules, adjudications)
    report.verify(assembled, bundle["priced"], bundle["planned"], Path(args.schema))
    _write(args.out, assembled)
    return {"lines": assembled["summary"]["line_count"], "counts": assembled["summary"]["counts_by_disposition"],
            "memo_items": len(report.memo_items(assembled))}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="freight", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("discover")
    for flag in ("--invoices", "--carriers", "--period", "--out", "--documents"):
        p.add_argument(flag, required=True)
    p = sub.add_parser("clauses")
    p.add_argument("--contract", required=True)
    p.add_argument("--out", required=True)
    p = sub.add_parser("check-spec")
    p.add_argument("--spec", required=True)
    p.add_argument("--clauses")
    p = sub.add_parser("price")
    for flag in ("--manifest", "--documents", "--shipments", "--policy", "--out"):
        p.add_argument(flag, required=True)
    p.add_argument("--spec", action="append", required=True, help="CARRIER=PATH")
    p = sub.add_parser("assemble")
    for flag in ("--priced", "--policy", "--schema", "--out"):
        p.add_argument(flag, required=True)
    p.add_argument("--adjudications")
    return parser


COMMANDS = {"discover": cmd_discover, "clauses": cmd_clauses, "check-spec": cmd_check_spec, "price": cmd_price,
            "assemble": cmd_assemble}
ERRORS = (documents.DiscoveryError, documents.ParseError, contracts.ContractError, ratespec.SpecError,
          pricing.PricingError, policy.PolicyError, report.AssemblyError, OSError, json.JSONDecodeError)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = COMMANDS[args.command](args)
    except ERRORS as exc:
        problems = getattr(exc, "problems", None)
        print(json.dumps({"error": {"type": type(exc).__name__, "message": str(exc),
                                    **({"problems": problems} if problems else {})}}))
        return 1
    print(json.dumps({"ok": True, **result}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
