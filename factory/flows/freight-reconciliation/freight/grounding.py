"""Grounding: every figure an agent writes must be copied from the facts it was given.

Agents never compute money. Their prose (adjudication justifications, memos) may quote figures, and
this check makes sure each one exists in the facts packet the agent received:

  - an amount marked as money (INR, Rs, ₹) must equal an amount in the facts (sign ignored);
  - any other number must appear in the facts, except small counting integers (0-12);
  - identifiers (FF-1234, ALPINE-0726, SHP00123), dates (2026-07-03), clause references (§3) and item
    references (#2) are not figures.

Numbers in the facts are collected generously (every digit run in every value), so the check rejects
invented or computed figures without rejecting a faithful quotation.
"""

import re
from decimal import Decimal, InvalidOperation

MONEY = re.compile(r"(?:\bINR|\bRs\.?|₹)\s*(-?\d[\d,]*(?:\.\d+)?)", re.IGNORECASE)
FIGURE = re.compile(r"(?<![\w/#.§\-])-?((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)(?![\w/#\-]|\.\d)")
LOOSE = re.compile(r"\d[\d,]*(?:\.\d+)?")
SMALL_INTEGER_LIMIT = 12


def _decimal(raw: str) -> Decimal | None:
    try:
        return abs(Decimal(raw.replace(",", "").rstrip(",")))
    except InvalidOperation:
        return None


def fact_numbers(*facts) -> set[Decimal]:
    out: set[Decimal] = set()

    def walk(value):
        if isinstance(value, bool) or value is None:
            return
        if isinstance(value, (int, float)):
            out.add(abs(Decimal(str(value))))
        elif isinstance(value, str):
            for m in LOOSE.finditer(value):
                if (d := _decimal(m.group(0).rstrip(","))) is not None:
                    out.add(d)
        elif isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, (list, tuple)):
            for v in value:
                walk(v)

    for fact in facts:
        walk(fact)
    return out


def figures(text: str) -> list[tuple[str, Decimal, bool]]:
    """(as written, value, marked as money) for every figure in the text."""
    found = []
    blanked = list(text)
    for m in MONEY.finditer(text):
        if (d := _decimal(m.group(1))) is not None:
            found.append((m.group(0), d, True))
        blanked[m.start():m.end()] = " " * (m.end() - m.start())
    for m in FIGURE.finditer("".join(blanked)):
        if (d := _decimal(m.group(1))) is not None:
            found.append((m.group(0), d, False))
    return found


def check(text: str, allowed: set[Decimal], where: str) -> list[str]:
    problems = []
    for written, value, money in figures(text):
        if value in allowed:
            continue
        if money:
            problems.append(f"{where}: amount {written!r} is not an amount in the facts provided; quote amounts "
                            "exactly as given and do not calculate new ones")
        elif not (value == value.to_integral_value() and value <= SMALL_INTEGER_LIMIT):
            problems.append(f"{where}: number {written!r} does not appear in the facts provided")
    return problems
