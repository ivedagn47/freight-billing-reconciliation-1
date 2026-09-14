"""Discovery: parse every invoice file, attach carriers, and decide what a run reconciles.

Scope rule (confirmed before implementation):
  - invoices whose billing period is the run's period are in scope;
  - credit notes that correct an in-scope invoice are in scope, whatever their issue date;
  - every other document is parsed and kept for reference (e.g. duplicate billing across
    periods) with the reason it is out of scope.
Documents whose period or carrier cannot be established are listed as unresolved, never
silently dropped.
"""

import re
from pathlib import Path

import yaml

from . import parsers
from .parsers.base import ParseError, check

PERIOD = re.compile(r"^\d{4}-\d{2}$")


class DiscoveryError(Exception):
    pass


def load_carriers(path: Path) -> dict:
    data = yaml.safe_load(Path(path).read_text()) or {}
    carriers = data.get("carriers")
    if not isinstance(carriers, dict) or not carriers:
        raise DiscoveryError(f"{path}: no carriers defined")
    seen_formats = {}
    for cid, spec in carriers.items():
        for key in ("name", "contract", "consignment_prefix", "invoice_formats"):
            if key not in spec:
                raise DiscoveryError(f"{path}: carrier {cid!r} is missing {key}")
        for fmt in spec["invoice_formats"]:
            if fmt not in parsers.FORMATS:
                raise DiscoveryError(f"{path}: carrier {cid!r} uses unknown format {fmt!r}")
            if fmt in seen_formats:
                raise DiscoveryError(f"{path}: format {fmt!r} belongs to both {seen_formats[fmt]} and {cid}")
            seen_formats[fmt] = cid
    return carriers


def carrier_for_format(carriers: dict, fmt: str) -> str:
    for cid, spec in carriers.items():
        if fmt in spec["invoice_formats"]:
            return cid
    raise DiscoveryError(f"no carrier is configured for format {fmt!r}")


def parse_file(path: Path, carriers: dict) -> list[dict]:
    docs = parsers.parse_file(path)
    for doc in docs:
        doc["carrier"] = carrier_for_format(carriers, doc["format"])
        prefix = carriers[doc["carrier"]]["consignment_prefix"]
        for line in doc["lines"]:
            check(doc, "consignment_prefix", line["consignment_ref"].startswith(prefix),
                  f"consignment reference does not start with {prefix!r}", line["line_no"], prefix,
                  line["consignment_ref"])
    return docs


def discover(invoice_dir: Path, carriers: dict) -> list[dict]:
    files = sorted(p for p in Path(invoice_dir).iterdir() if p.is_file() and not p.name.startswith("."))
    docs, seen = [], {}
    for path in files:
        for doc in parse_file(path, carriers):
            if doc["doc_id"] in seen:
                raise DiscoveryError(f"document id {doc['doc_id']!r} appears in both {seen[doc['doc_id']]} and {path}")
            seen[doc["doc_id"]] = str(path)
            docs.append(doc)
    return docs


def manifest(docs: list[dict], period: str, carriers: dict) -> dict:
    if not PERIOD.match(period):
        raise DiscoveryError(f"period {period!r} is not YYYY-MM")
    by_id = {d["doc_id"]: d for d in docs}
    in_scope_invoices = {d["doc_id"] for d in docs if d["doc_type"] == "invoice" and d["billing_period"] == period}
    entries, in_scope, reference, unresolved = [], [], [], []
    for doc in sorted(docs, key=lambda d: (d["carrier"], d["doc_type"] != "invoice", d["doc_id"])):
        entry = {k: doc[k] for k in ("doc_id", "doc_type", "carrier", "format", "source_file", "source_sha256",
                                     "billing_period", "issue_date", "against_invoice")}
        entry.update(line_count=len(doc["lines"]), stated_total=doc["stated"]["total"],
                     integrity_failures=len(doc["integrity"]["failures"]))
        if doc["doc_type"] == "invoice":
            if doc["billing_period"] is None:
                entry["scope"], entry["reason"] = "unresolved", "billing period could not be established"
            elif doc["doc_id"] in in_scope_invoices:
                entry["scope"], entry["reason"] = "in_scope", f"invoice for {period}"
            else:
                entry["scope"], entry["reason"] = "reference", f"invoice for {doc['billing_period']}"
        else:
            target = doc["against_invoice"]
            if target in in_scope_invoices:
                entry["scope"], entry["reason"] = "in_scope", f"credit note correcting in-scope invoice {target}"
            elif target in by_id:
                entry["scope"], entry["reason"] = "reference", \
                    f"credit note correcting {target} (billing period {by_id[target]['billing_period']})"
            else:
                entry["scope"], entry["reason"] = "unresolved", f"credit note corrects unknown invoice {target}"
        {"in_scope": in_scope, "reference": reference, "unresolved": unresolved}[entry["scope"]].append(doc["doc_id"])
        entries.append(entry)

    carrier_rows = {}
    for cid, spec in carriers.items():
        carrier_rows[cid] = {"name": spec["name"], "contract": spec["contract"],
                             "in_scope": [e["doc_id"] for e in entries if e["carrier"] == cid and e["scope"] == "in_scope"]}
    return {"period": period, "documents": entries, "in_scope": in_scope, "reference": reference,
            "unresolved": unresolved, "carriers": carrier_rows}


__all__ = ["DiscoveryError", "ParseError", "carrier_for_format", "discover", "load_carriers", "manifest", "parse_file"]
