"""Normalized document shape shared by every format parser.

    {doc_id, id_source, doc_type: invoice|credit_note, format, carrier, source_file, source_sha256,
     billing_period: "YYYY-MM"|null, period: {start, end, label}|null, issue_date, against_invoice,
     agreement_ref, stated: {total, line_count, discount},
     lines: [{line_no, consignment_ref, booking_date, attributes, charges: [{code, label, amount,
              derived}], stated_total, note, source: {first_line, last_line}}],
     integrity: {checks, failures: [{check, message, line_no, expected, actual}]}}

`attributes` are what the invoice *states* (weight, distance, route, service, rate). They are
kept as evidence and compared with shipment records; they are never used to price a line.
Parsers are strict: text they cannot interpret is a ParseError, not a guess.
"""

import hashlib
from pathlib import Path


class ParseError(Exception):
    def __init__(self, source: Path | str, message: str, line: int | None = None):
        where = f"{source}:{line}" if line else str(source)
        super().__init__(f"{where}: {message}")
        self.source, self.line, self.message = str(source), line, message


def new_document(fmt: str, path: Path) -> dict:
    return {
        "doc_id": None, "id_source": "document", "doc_type": None, "format": fmt, "carrier": None,
        "source_file": str(path), "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "billing_period": None, "period": None, "issue_date": None, "against_invoice": None,
        "agreement_ref": None, "stated": {"total": None, "line_count": None, "discount": None},
        "lines": [], "integrity": {"checks": 0, "failures": []},
    }


def new_line(line_no: int, consignment_ref: str, first_line: int | None = None) -> dict:
    return {"line_no": line_no, "consignment_ref": consignment_ref, "booking_date": None, "attributes": {},
            "charges": [], "stated_total": None, "note": None,
            "source": {"first_line": first_line, "last_line": first_line}}


def check(doc: dict, name: str, ok: bool, message: str, line_no: int | None = None,
          expected=None, actual=None) -> None:
    doc["integrity"]["checks"] += 1
    if not ok:
        doc["integrity"]["failures"].append({
            "check": name, "message": message, "line_no": line_no,
            "expected": None if expected is None else str(expected),
            "actual": None if actual is None else str(actual)})
