"""Agreement between independently extracted rate specs, judged by what they do, not how they are written.

Two careful readings of one contract rarely match textually: components get different names, orders
and descriptions. What matters is whether the pricing engine would behave identically, so compare()
evaluates both specs on the same probe shipments and compares, per probe:

  - the rounded amount (or that it is undetermined) and the evaluation flags (gap vs outside the card);
  - for every charge code the carrier's invoices can carry: uncontracted, applies, not applicable, or
    unverifiable (it depends on a fact the shipment records do not hold);
  - whether the shipment's service level is offered;

and, independently of shipments, the term and the invoice adjustments at consignment counts around
every threshold either spec uses.

Probes: for each numeric field either spec reads, the values 0, every threshold in either spec (band
ends and quantity constants), two points inside each interval between consecutive thresholds, and two
beyond the last; crossed with every service level and every combination of handling flags that either
spec or the shipment vocabulary mentions. Amounts are linear in each field between thresholds, so
agreement at these points means agreement everywhere, provided quantities combine a field with
constants (max/min of two different fields bends along a diagonal the grid does not sample).
"""

import itertools
from decimal import Decimal

from . import pricing
from .money import fmt, to_decimal
from .tracing import _condition_values, canonical
from .vocab import CHARGE_CODES, SHIPMENT_FIELDS

NUMERIC_FIELDS = tuple(k for k, t in SHIPMENT_FIELDS.items() if t == "number")
COMPARED_CODES = tuple(sorted(set(CHARGE_CODES) - {"credit", "other"}))
MAX_PROBES = 100_000
MAX_DIFFERENCES = 20
MAX_FLAGS_FOR_ALL_SUBSETS = 6
QUANTUM = Decimal("0.001")


class AgreementError(Exception):
    pass


def _refs(spec: dict):
    for q in spec["quantities"]:
        yield from q["args"]
    for c in spec["components"]:
        for key in ("basis", "select_by"):
            if key in c["calc"]:
                yield c["calc"][key]


def _thresholds(spec: dict) -> set[Decimal]:
    out = {to_decimal(r["const"]) for r in _refs(spec) if "const" in r}
    for c in spec["components"]:
        for band in c["calc"].get("bands", []):
            out |= {to_decimal(band[k]) for k in ("min", "max") if band[k] is not None}
    return out


def _fields(spec: dict) -> set[str]:
    return {r["field"] for r in _refs(spec) if "field" in r}


def grid(thresholds: set[Decimal]) -> list[Decimal]:
    points = sorted(thresholds | {Decimal(0)})
    values = set(points)
    for low, high in zip(points, points[1:]):
        step = (high - low) / 3
        values |= {(low + step).quantize(QUANTUM), (low + 2 * step).quantize(QUANTUM)}
    top = points[-1]
    values |= {top + 1, top * 2 + 7}
    return sorted(values)


def _handling_sets(flags: set[str]) -> list[list[str]]:
    ordered = sorted(flags)
    if len(ordered) <= MAX_FLAGS_FOR_ALL_SUBSETS:
        return [list(c) for r in range(len(ordered) + 1) for c in itertools.combinations(ordered, r)]
    return [[]] + [[f] for f in ordered] + [ordered]


def probes(a: dict, b: dict, vocabulary: dict | None = None) -> list[dict]:
    fields = sorted(_fields(a) | _fields(b))
    values = grid(_thresholds(a) | _thresholds(b))
    services, flags = set(), set()
    for spec in (a, b):
        services.update(spec["service_levels"]["allowed"])
        mentioned: list[tuple[str, str]] = []
        for c in spec["components"]:
            if "when" in c:
                _condition_values(c["when"], mentioned)
        for kind, value in mentioned:
            (services if kind == "service_level" else flags).add(value)
    if vocabulary:
        services |= set(vocabulary["service_level"])
        flags |= set(vocabulary["special_handling"])
    handling = _handling_sets(flags)
    count = len(values) ** len(fields) * max(len(services), 1) * len(handling)
    if count > MAX_PROBES:
        raise AgreementError(f"{count} probe shipments needed (limit {MAX_PROBES}); compare these specs by hand")
    base = {f: Decimal(1) for f in NUMERIC_FIELDS}
    return [{**base, **dict(zip(fields, combo)), "service_level": service, "special_handling": list(h)}
            for combo in itertools.product(values, repeat=len(fields))
            for service in sorted(services) or [""]
            for h in handling]


def outcome(spec: dict, shipment: dict, compared_codes=COMPARED_CODES) -> dict:
    """What the pricing engine does with this shipment under this spec (mirrors pricing._charge_flags)."""
    evaluation = pricing.evaluate(spec, shipment)
    applied = {r["name"] for r in evaluation["components"] if r["applied"]}
    unverifiable = {r["name"] for r in evaluation["components"] if r.get("unverifiable")}
    codes = {}
    for code in compared_codes:
        matched = [c["name"] for c in spec["components"] if code in c["charge_codes"]]
        if not matched:
            codes[code] = "uncontracted"
        elif not evaluation["components"]:
            codes[code] = "unknown"
        elif any(m in applied for m in matched):
            codes[code] = "applies"
        else:
            codes[code] = "unverifiable" if any(m in unverifiable for m in matched) else "not_applicable"
    return {"amount": None if evaluation["amount"] is None else fmt(evaluation["amount"]),
            "flags": sorted({f["code"] for f in evaluation["flags"]}),
            "charge_codes": codes,
            "service_offered": shipment["service_level"] in spec["service_levels"]["allowed"]}


def _adjustments(spec: dict, count: int) -> list[list[str]]:
    return sorted([a["kind"], canonical(a["calc"]["percent"])] for a in spec["invoice_adjustments"]
                  if pricing._count_condition(a["when"]["consignments_in_billing_month"], count))


def _only_differences(mine: dict, theirs: dict) -> dict:
    out = {}
    for key, value in mine.items():
        if key == "charge_codes":
            codes = {c: v for c, v in value.items() if theirs[key][c] != v}
            if codes:
                out[key] = codes
        elif theirs[key] != value:
            out[key] = value
    return out


def _show(shipment: dict, fields: set[str]) -> dict:
    return {k: (canonical(v) if isinstance(v, Decimal) else v) for k, v in shipment.items()
            if k in fields or not isinstance(v, Decimal)}


def compare(a: dict, b: dict, vocabulary: dict | None = None, invoice_codes: list[str] | None = None) -> dict:
    """invoice_codes: the charge codes the carrier's invoices can carry. Only those are compared; how a
    spec maps a code the carrier never bills cannot change any price."""
    compared_codes = COMPARED_CODES if invoice_codes is None else tuple(sorted(set(invoice_codes) & set(COMPARED_CODES)))
    differences, total = [], 0

    def record(entry: dict) -> None:
        nonlocal total
        total += 1
        if len(differences) < MAX_DIFFERENCES:
            differences.append(entry)

    if (a["term"]["start"], a["term"]["end"]) != (b["term"]["start"], b["term"]["end"]):
        record({"aspect": "term", "a": a["term"], "b": b["term"]})
    bounds = {v for s in (a, b) for adj in s["invoice_adjustments"]
              for v in adj["when"]["consignments_in_billing_month"].values()}
    for n in sorted({0} | {x for v in bounds for x in (v - 1, v, v + 1) if x >= 0}):
        if _adjustments(a, n) != _adjustments(b, n):
            record({"aspect": "invoice_adjustments", "consignments_in_billing_month": n,
                    "a": _adjustments(a, n), "b": _adjustments(b, n)})

    points = probes(a, b, vocabulary)
    fields = _fields(a) | _fields(b)
    for shipment in points:
        mine, theirs = outcome(a, shipment, compared_codes), outcome(b, shipment, compared_codes)
        if mine != theirs:
            record({"aspect": "shipment", "shipment": _show(shipment, fields),
                    "a": _only_differences(mine, theirs), "b": _only_differences(theirs, mine)})
    return {"agree": total == 0, "probes": len(points), "differences_total": total, "differences": differences}
