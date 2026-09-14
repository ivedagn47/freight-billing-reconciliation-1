"""Alpine Express JSON invoices."""

import json
import re
from decimal import Decimal
from pathlib import Path

from ..money import fmt, round_inr, to_decimal
from .base import ParseError, check, new_document, new_line

NAME = "alpine-json"
PERIOD = re.compile(r"^\d{4}-\d{2}$")
REQUIRED = ("invoice_no", "billing_period", "lines", "invoice_total")
LINE_REQUIRED = ("consignment_no", "line_amount")


def sniff(path: Path, head: str) -> bool:
    return path.suffix.lower() == ".json" and '"Alpine Express' in head


def parse(path: Path, text: str) -> list[dict]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ParseError(path, f"invalid JSON: {exc}") from exc
    missing = [k for k in REQUIRED if k not in data]
    if missing:
        raise ParseError(path, f"missing fields {missing}")
    if not PERIOD.match(str(data["billing_period"])):
        raise ParseError(path, f"billing_period {data['billing_period']!r} is not YYYY-MM")

    doc = new_document(NAME, path)
    discount = round_inr(data.get("discount") or 0)
    doc.update(doc_id=data["invoice_no"], doc_type="invoice", billing_period=data["billing_period"])
    doc["stated"] = {"total": fmt(data["invoice_total"]), "line_count": data.get("consignment_count"),
                     "discount": fmt(discount)}

    for index, raw in enumerate(data["lines"], 1):
        missing = [k for k in LINE_REQUIRED if k not in raw]
        if missing:
            raise ParseError(path, f"line {index} missing fields {missing}")
        item = new_line(raw.get("sl", index), raw["consignment_no"])
        item["booking_date"] = raw.get("booking_date")
        amount, handling = round_inr(raw["line_amount"]), round_inr(raw.get("handling_fee") or 0)
        item["attributes"] = {k: raw[k] for k in ("actual_weight_kg", "chargeable_weight_kg", "rate_per_kg")
                              if k in raw}
        item["charges"].append({"code": "freight", "label": "rate_per_kg x chargeable_weight_kg",
                                "amount": fmt(amount - handling), "derived": True})
        if handling:
            item["charges"].append({"code": "handling", "label": "handling_fee", "amount": fmt(handling),
                                    "derived": False})
        item["stated_total"] = fmt(amount)
        doc["lines"].append(item)
        check(doc, "line_numbering", item["line_no"] == index, "line numbers are not consecutive",
              item["line_no"], index, item["line_no"])
        if "rate_per_kg" in raw and "chargeable_weight_kg" in raw:
            computed = round_inr(to_decimal(raw["rate_per_kg"]) * to_decimal(raw["chargeable_weight_kg"])) + handling
            check(doc, "line_amount_equals_rate_x_weight_plus_handling", computed == amount,
                  "the invoice's own rate x chargeable weight + handling does not equal its line amount",
                  item["line_no"], fmt(computed), fmt(amount))

    lines_total = sum((to_decimal(i["stated_total"]) for i in doc["lines"]), Decimal(0))
    check(doc, "document_total_equals_lines_less_discount",
          round_inr(lines_total - discount) == to_decimal(doc["stated"]["total"]),
          "line amounts less the stated discount do not equal the stated invoice total", None,
          fmt(lines_total - discount), doc["stated"]["total"])
    if doc["stated"]["line_count"] is not None:
        check(doc, "line_count_matches_stated", doc["stated"]["line_count"] == len(doc["lines"]),
              "consignment_count does not match the number of lines", None,
              doc["stated"]["line_count"], len(doc["lines"]))
    return [doc]
