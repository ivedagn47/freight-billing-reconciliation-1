"""Rate spec: a contract's pricing rules as data, and the checks JSON Schema cannot express.

Structure (definitions/rate-spec.json):
  quantities           derived values, e.g. chargeable weight = max(billed_weight_kg, 25)
  components           amounts per consignment, evaluated in order:
                         flat{amount} | per_unit{basis, rate} | banded_rate{basis, select_by, bands}
                         | percent_of{percent, of: [earlier components]}
                       each optionally gated by a shipment condition and naming the invoice
                       charge codes it accounts for
  service_levels       services the contract offers
  invoice_adjustments  invoice-level discounts gated on consignments tendered in the billing month
  gaps                 what the contract leaves undetermined (informational)
  non_pricing          clauses that do not affect a price, with the reason
  unrepresentable      pricing terms the building blocks cannot express (any entry stops the run)

Bands are written exactly as the contract words them (inclusive or exclusive ends). A value
that falls in no band is never snapped to the nearest one: pricing reports it as a gap.
"""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import jsonschema

from .money import to_decimal

SPEC_VERSION = 1
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "definitions" / "rate-spec.json"
NON_PRICING_KEYS = {"_session_id", "description", "clauses", "gaps", "non_pricing", "unrepresentable", "contract_file",
                    "agreement_ref"}
CITING_GROUPS = ("quantities", "components", "invoice_adjustments", "gaps", "non_pricing", "unrepresentable")


class SpecError(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def in_band(value: Decimal, band: dict) -> bool:
    if band["min"] is not None:
        low = to_decimal(band["min"])
        if value < low or (value == low and not band["min_inclusive"]):
            return False
    if band["max"] is not None:
        high = to_decimal(band["max"])
        if value > high or (value == high and not band["max_inclusive"]):
            return False
    return True


def _bands_problems(where: str, bands: list[dict]) -> list[str]:
    problems = []
    for i, b in enumerate(bands):
        if b["min"] is not None and b["max"] is not None:
            low, high = to_decimal(b["min"]), to_decimal(b["max"])
            if low > high or (low == high and not (b["min_inclusive"] and b["max_inclusive"])):
                problems.append(f"{where}: band {i} is empty")
    ordered = sorted(bands, key=lambda b: (b["min"] is not None, to_decimal(b["min"] or 0)))
    for a, b in zip(ordered, ordered[1:]):
        if a["max"] is None or b["min"] is None:
            problems.append(f"{where}: bands overlap (an open-ended band is not the last one)")
            continue
        a_max, b_min = to_decimal(a["max"]), to_decimal(b["min"])
        if a_max > b_min or (a_max == b_min and a["max_inclusive"] and b["min_inclusive"]):
            problems.append(f"{where}: bands overlap at {a_max}")
    return problems


def _ref_problems(where: str, ref: dict, quantities: set[str]) -> list[str]:
    if "quantity" in ref and ref["quantity"] not in quantities:
        return [f"{where}: quantity {ref['quantity']!r} is not defined before it is used"]
    return []


def validate(spec: dict) -> dict:
    errors = sorted(jsonschema.Draft202012Validator(schema()).iter_errors(spec),
                    key=lambda e: [str(p) for p in e.absolute_path])
    if errors:
        raise SpecError([f"/{'/'.join(str(p) for p in e.absolute_path)}: {e.message}" for e in errors])

    problems: list[str] = []
    names: set[str] = set()

    def claim(name: str, where: str) -> None:
        if name in names:
            problems.append(f"{where}: name {name!r} is used more than once")
        names.add(name)

    if date.fromisoformat(spec["term"]["start"]) > date.fromisoformat(spec["term"]["end"]):
        problems.append("term: start is after end")

    quantities: set[str] = set()
    for i, q in enumerate(spec["quantities"]):
        where = f"quantities[{i}] {q['name']}"
        for ref in q["args"]:
            problems += _ref_problems(where, ref, quantities)
        claim(q["name"], where)
        quantities.add(q["name"])

    components: set[str] = set()
    for i, c in enumerate(spec["components"]):
        where = f"components[{i}] {c['name']}"
        calc = c["calc"]
        for key in ("basis", "select_by"):
            if key in calc:
                problems += _ref_problems(f"{where}.{key}", calc[key], quantities)
        if calc["op"] == "banded_rate":
            problems += _bands_problems(where, calc["bands"])
        if calc["op"] == "percent_of":
            for target in calc["of"]:
                if target not in components:
                    problems.append(f"{where}: percent_of refers to {target!r}, which is not an earlier component")
        claim(c["name"], where)
        components.add(c["name"])

    for i, a in enumerate(spec["invoice_adjustments"]):
        claim(a["name"], f"invoice_adjustments[{i}] {a['name']}")

    if problems:
        raise SpecError(problems)
    return spec


def load(path: Path) -> dict:
    return validate(json.loads(Path(path).read_text()))


def pricing_view(spec: dict):
    """The parts of a spec that change a price, with numbers normalised; for comparing two
    independently produced specs. Clause citations, descriptions and gaps are excluded."""
    def strip(value):
        if isinstance(value, dict):
            return {k: strip(v) for k, v in sorted(value.items()) if k not in NON_PRICING_KEYS}
        if isinstance(value, list):
            return [strip(v) for v in value]
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return format(to_decimal(value).normalize(), "f")
        return value
    view = strip(spec)
    view["service_levels"]["allowed"] = sorted(view["service_levels"]["allowed"])
    return view


def cited_clauses(spec: dict) -> dict[str, list[str]]:
    """Every clause citation in the spec, keyed by the element that makes it."""
    out = {"term": spec["term"]["clauses"], "service_levels": spec["service_levels"]["clauses"]}
    for group in CITING_GROUPS:
        for i, element in enumerate(spec[group]):
            out[f"{group}[{i}]{'.' + element['name'] if 'name' in element else ''}"] = element["clauses"]
    return out
