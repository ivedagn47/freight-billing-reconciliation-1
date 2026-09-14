"""Falcon Freight fixed-layout text invoices and credit notes."""

import re
from decimal import Decimal
from pathlib import Path

from ..money import fmt, round_inr, to_decimal
from .base import ParseError, check, new_document, new_line

NAME = "falcon-text"

INVOICE = re.compile(r"^TAX INVOICE (?P<id>\S+)\s+Period: (?P<d1>\d{1,2})-(?P<d2>\d{1,2}) (?P<ym>\d{4}-\d{2})$")
CREDIT_NOTE = re.compile(r"^CREDIT NOTE (?P<id>\S+)\s+Date: (?P<date>\d{4}-\d{2}-\d{2})$")
AGAINST = re.compile(r"^Against: TAX INVOICE (?P<id>\S+)$")
AGREEMENT = re.compile(r"under agreement (?P<ref>\S+)$")
ITEM = re.compile(r"^(?P<no>\d+)\.\s+Consignment (?P<ref>\S+)$")
ROUTE = re.compile(r"^(?P<origin>.+?) to (?P<dest>.+?), (?P<km>[\d,]+(?:\.\d+)?) km, "
                   r"(?P<kg>[\d,]+(?:\.\d+)?) kg, (?P<service>[a-z]+)$")
CHARGE = re.compile(r"^(?P<label>[^:]+): Rs (?P<amount>-?[\d,]+\.\d{2})$")
TOTAL = re.compile(r"^(?:INVOICE|CREDIT NOTE) TOTAL: Rs (?P<amount>-?[\d,]+\.\d{2})$")
IGNORED = (re.compile(r"^=+$"), re.compile(r"^FALCON FREIGHT PVT LTD$"), re.compile(r"^Payment due .*$"))

LABELS = {
    "Freight incl. fuel surcharge": "freight_incl_fuel",
    "Freight": "freight",
    "Fuel surcharge": "fuel_surcharge",
    "Express premium": "express_premium",
    "Residential delivery": "residential_delivery",
    "Detention charge at consignee": "detention",
}


def sniff(path: Path, head: str) -> bool:
    return path.suffix.lower() == ".txt" and head.lstrip().startswith("FALCON FREIGHT PVT LTD")


def _number(text: str):
    value = to_decimal(text)
    return int(value) if value == value.to_integral_value() else float(value)


def parse(path: Path, text: str) -> list[dict]:
    doc = new_document(NAME, path)
    current = None
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or any(p.match(line) for p in IGNORED):
            continue
        if m := INVOICE.match(line):
            ym = m["ym"]
            doc.update(doc_id=m["id"], doc_type="invoice", billing_period=ym,
                       period={"start": f"{ym}-{int(m['d1']):02d}", "end": f"{ym}-{int(m['d2']):02d}",
                               "label": f"{m['d1']}-{m['d2']} {ym}"})
            continue
        if m := CREDIT_NOTE.match(line):
            doc.update(doc_id=m["id"], doc_type="credit_note", issue_date=m["date"])
            continue
        if m := AGAINST.match(line):
            doc["against_invoice"] = m["id"]
            continue
        if m := AGREEMENT.search(line):
            doc["agreement_ref"] = m["ref"]
            continue
        if m := ITEM.match(line):
            current = new_line(int(m["no"]), m["ref"], number)
            doc["lines"].append(current)
            continue
        if m := TOTAL.match(line):
            doc["stated"]["total"] = fmt(to_decimal(m["amount"]))
            current = None
            continue
        if current is None:
            raise ParseError(path, f"unexpected text outside a consignment block: {line!r}", number)
        current["source"]["last_line"] = number
        if m := ROUTE.match(line):
            current["attributes"].update(origin=m["origin"], destination=m["dest"], distance_km=_number(m["km"]),
                                         weight_kg=_number(m["kg"]), service_level=m["service"])
        elif m := CHARGE.match(line):
            label, amount = m["label"].strip(), round_inr(to_decimal(m["amount"]))
            if label == "LINE TOTAL":
                current["stated_total"] = fmt(amount)
            else:
                current["charges"].append({"code": LABELS.get(label, "other"), "label": label,
                                           "amount": fmt(amount), "derived": False})
        else:
            current["note"] = f"{current['note']} {line}" if current["note"] else line

    if not doc["doc_id"]:
        raise ParseError(path, "no TAX INVOICE or CREDIT NOTE header found")
    if doc["stated"]["total"] is None:
        raise ParseError(path, "no INVOICE TOTAL / CREDIT NOTE TOTAL line found")
    if doc["doc_type"] == "credit_note" and not doc["against_invoice"]:
        raise ParseError(path, "credit note without an 'Against: TAX INVOICE' line")

    for index, item in enumerate(doc["lines"], 1):
        if item["stated_total"] is None:
            raise ParseError(path, f"consignment {item['consignment_ref']} has no LINE TOTAL",
                             item["source"]["first_line"])
        if doc["doc_type"] == "credit_note" and not item["charges"]:
            item["charges"].append({"code": "credit", "label": "credit", "amount": item["stated_total"],
                                    "derived": True})
        check(doc, "line_numbering", item["line_no"] == index, "line numbers are not consecutive",
              item["line_no"], index, item["line_no"])
        if doc["doc_type"] == "invoice":
            check(doc, "line_attributes_present", {"distance_km", "weight_kg"} <= set(item["attributes"]),
                  "route/weight line missing", item["line_no"])
        charges = sum((to_decimal(c["amount"]) for c in item["charges"]), Decimal(0))
        check(doc, "line_total_equals_charges", charges == to_decimal(item["stated_total"]),
              "charges do not add up to the stated line total", item["line_no"], fmt(charges), item["stated_total"])
    lines_total = sum((to_decimal(i["stated_total"]) for i in doc["lines"]), Decimal(0))
    check(doc, "document_total_equals_lines", lines_total == to_decimal(doc["stated"]["total"]),
          "line totals do not add up to the stated document total", None, fmt(lines_total), doc["stated"]["total"])
    return [doc]
