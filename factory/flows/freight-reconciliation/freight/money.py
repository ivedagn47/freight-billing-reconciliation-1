"""Exact INR arithmetic.

Amounts are `Decimal` in code and decimal strings ("17472.00") in intermediate JSON, so no
binary floating point ever touches money. Rounding is half-up to the paisa, applied once per
computed line total (components are summed exactly first).
"""

import re
from decimal import ROUND_HALF_UP, Decimal

PAISE = Decimal("0.01")
_PLAIN = re.compile(r"^-?\d+(\.\d+)?$")


def to_decimal(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise ValueError(f"not an amount: {value!r}")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value))
    if isinstance(value, str):
        text = value.strip()
        if text[:2].lower() == "rs":
            text = text[2:]
        text = text.replace("₹", "").replace(",", "").strip()
        if _PLAIN.match(text):
            return Decimal(text)
    raise ValueError(f"not an amount: {value!r}")


def round_inr(value) -> Decimal:
    return to_decimal(value).quantize(PAISE, rounding=ROUND_HALF_UP)


def fmt(value) -> str:
    return str(round_inr(value))


def to_number(value) -> float:
    return float(round_inr(value))


def within(a, b, tolerance) -> bool:
    return abs(to_decimal(a) - to_decimal(b)) <= to_decimal(tolerance)
