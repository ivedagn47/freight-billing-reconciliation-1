"""Sagar Roadlines CSV invoices and credit notes.

The invoice CSV carries no invoice number, so the id comes from the file name (recorded as
`id_source: filename`), and the billing period from the booking dates.
"""

import csv
import io
import re
from decimal import Decimal
from pathlib import Path

from ..money import fmt, round_inr, to_decimal
from .base import ParseError, check, new_document, new_line

NAME = "sagar-csv"
INVOICE_HEADER = ["cnote_no", "booking_dt", "wt_kg", "dist_km", "freight_rs", "chill_prem_rs", "total_rs"]
CREDIT_HEADER = ["credit_note", "against_invoice", "cnote_no", "credit_rs"]
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _header(head: str) -> list[str]:
    first = head.splitlines()[0] if head else ""
    return [c.strip() for c in first.split(",")]


def sniff(path: Path, head: str) -> bool:
    return path.suffix.lower() == ".csv" and _header(head) in (INVOICE_HEADER, CREDIT_HEADER)


def _rows(path: Path, text: str) -> tuple[list[tuple[int, dict]], str | None]:
    reader = csv.DictReader(io.StringIO(text))
    rows, total = [], None
    for number, row in enumerate(reader, 2):
        first = next(iter(row.values()), "")
        if (first or "").strip().upper() == "TOTAL":
            values = [v for v in row.values() if v and v.strip() and v.strip().upper() != "TOTAL"]
            if len(values) != 1:
                raise ParseError(path, "TOTAL row must carry exactly one amount", number)
            total = fmt(values[0])
            continue
        if total is not None:
            raise ParseError(path, "rows after the TOTAL row", number)
        rows.append((number, row))
    return rows, total


def _number(text: str, path: Path, number: int, field: str):
    try:
        value = to_decimal(text)
    except ValueError as exc:
        raise ParseError(path, f"{field}={text!r} is not a number", number) from exc
    return int(value) if value == value.to_integral_value() else float(value)


def parse(path: Path, text: str) -> list[dict]:
    header = _header(text)
    rows, total = _rows(path, text)
    if total is None:
        raise ParseError(path, "no TOTAL row")
    return _parse_credit_notes(path, rows, total) if header == CREDIT_HEADER else \
        _parse_invoice(path, rows, total)


def _parse_invoice(path: Path, rows, total) -> list[dict]:
    doc = new_document(NAME, path)
    doc.update(doc_id=path.stem, id_source="filename", doc_type="invoice")
    doc["stated"]["total"] = total
    months = set()
    for index, (number, row) in enumerate(rows, 1):
        if not DATE.match((row["booking_dt"] or "").strip()):
            raise ParseError(path, f"booking_dt {row['booking_dt']!r} is not YYYY-MM-DD", number)
        item = new_line(index, row["cnote_no"].strip(), number)
        item["booking_date"] = row["booking_dt"].strip()
        months.add(item["booking_date"][:7])
        item["attributes"] = {"weight_kg": _number(row["wt_kg"], path, number, "wt_kg"),
                              "distance_km": _number(row["dist_km"], path, number, "dist_km")}
        freight, premium = round_inr(row["freight_rs"]), round_inr(row["chill_prem_rs"] or 0)
        item["charges"].append({"code": "freight", "label": "freight_rs", "amount": fmt(freight), "derived": False})
        if premium:
            item["charges"].append({"code": "cold_chain_premium", "label": "chill_prem_rs", "amount": fmt(premium),
                                    "derived": False})
        item["stated_total"] = fmt(row["total_rs"])
        doc["lines"].append(item)
        check(doc, "line_total_equals_charges", freight + premium == to_decimal(item["stated_total"]),
              "freight_rs + chill_prem_rs does not equal total_rs", index, fmt(freight + premium),
              item["stated_total"])
    check(doc, "single_billing_month", len(months) == 1, "booking dates span more than one month", None,
          "1 month", sorted(months))
    doc["billing_period"] = next(iter(months)) if len(months) == 1 else None
    lines_total = sum((to_decimal(i["stated_total"]) for i in doc["lines"]), Decimal(0))
    check(doc, "document_total_equals_lines", lines_total == to_decimal(total),
          "total_rs values do not add up to the TOTAL row", None, fmt(lines_total), total)
    return [doc]


def _parse_credit_notes(path: Path, rows, total) -> list[dict]:
    docs: dict[str, dict] = {}
    for number, row in rows:
        cn_id = row["credit_note"].strip()
        doc = docs.get(cn_id)
        if doc is None:
            doc = docs[cn_id] = new_document(NAME, path)
            doc.update(doc_id=cn_id, doc_type="credit_note", against_invoice=row["against_invoice"].strip())
        elif doc["against_invoice"] != row["against_invoice"].strip():
            raise ParseError(path, f"credit note {cn_id} corrects more than one invoice", number)
        item = new_line(len(doc["lines"]) + 1, row["cnote_no"].strip(), number)
        amount = round_inr(row["credit_rs"])
        item["charges"].append({"code": "credit", "label": "credit_rs", "amount": fmt(amount), "derived": False})
        item["stated_total"] = fmt(amount)
        doc["lines"].append(item)
    grand = Decimal(0)
    for doc in docs.values():
        doc_total = sum((to_decimal(i["stated_total"]) for i in doc["lines"]), Decimal(0))
        doc["stated"]["total"] = fmt(doc_total)
        grand += doc_total
    for doc in docs.values():
        check(doc, "file_total_equals_rows", grand == to_decimal(total),
              "credit_rs values do not add up to the TOTAL row", None, fmt(grand), total)
    return list(docs.values())
