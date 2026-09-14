"""Trace a rate spec back to the contract it claims to transcribe.

Schema validation proves a spec is well formed; this proves it is anchored in its contract:

  - identity: carrier, contract file, agreement ref and term match the clause index;
  - citations: every cited clause exists;
  - tracing: every number that changes a price appears in a clause the element cites
    (a rate of 9.5 citing a clause that says "₹9.50" passes; citing a clause without it fails);
  - coverage: every clause of the contract is cited somewhere (pricing element, gap, non_pricing
    or unrepresentable), so no clause is silently ignored;
  - vocabulary: conditions and service levels use values that occur in the shipment records, so a
    misspelt flag cannot make a component silently never apply.

It cannot prove the reading is right; two independent extractions agreeing (agreement.py) covers that.
"""

from datetime import date

from . import ratespec
from .contracts import numbers as text_numbers
from .money import to_decimal


def canonical(value) -> str:
    return format(to_decimal(value).normalize(), "f")


def _condition_values(cond: dict, out: list[tuple[str, str]]) -> None:
    if "service_level" in cond:
        out.append(("service_level", cond["service_level"]))
    elif "special_handling_includes" in cond:
        out.append(("special_handling", cond["special_handling_includes"]))
    elif "unrecorded" in cond:
        return  # a fact the records do not hold: there is no vocabulary to check it against
    elif "not" in cond:
        _condition_values(cond["not"], out)
    else:
        for sub in cond.get("all") or cond.get("any") or []:
            _condition_values(sub, out)


def _ref_numbers(label: str, ref: dict) -> list[tuple[str, object]]:
    return [(f"{label} const", ref["const"])] if "const" in ref else []


def priced_numbers(spec: dict) -> list[tuple[str, list[str], list[tuple[str, object]]]]:
    """(element, cited clauses, [(label, number)]) for every element whose numbers change a price."""
    out = []
    for i, q in enumerate(spec["quantities"]):
        values = [v for n, a in enumerate(q["args"]) for v in _ref_numbers(f"args[{n}]", a)]
        out.append((f"quantities[{i}] {q['name']}", q["clauses"], values))
    for i, c in enumerate(spec["components"]):
        calc, values = c["calc"], []
        for key in ("basis", "select_by"):
            if key in calc:
                values += _ref_numbers(key, calc[key])
        if calc["op"] == "flat":
            values.append(("amount", calc["amount"]))
        elif calc["op"] == "per_unit":
            values.append(("rate", calc["rate"]))
        elif calc["op"] == "percent_of":
            values.append(("percent", calc["percent"]))
        else:
            for n, band in enumerate(calc["bands"]):
                values += [(f"bands[{n}].{k}", band[k]) for k in ("min", "max", "rate") if band[k] is not None]
        out.append((f"components[{i}] {c['name']}", c["clauses"], values))
    for i, a in enumerate(spec["invoice_adjustments"]):
        (op, bound), = a["when"]["consignments_in_billing_month"].items()
        out.append((f"invoice_adjustments[{i}] {a['name']}", a["clauses"],
                    [(f"when {op}", bound), ("percent", a["calc"]["percent"])]))
    return out


def _header_numbers(index: dict) -> set[str]:
    text = " ".join(str(index.get(k) or "") for k in ("agreement_ref", "service"))
    return set(text_numbers(text + " " + ((index.get("term") or {}).get("text") or "")))


def check(spec: dict, index: dict, vocabulary: dict | None = None, carrier: str | None = None) -> list[str]:
    """Every problem found; an empty list means the spec is traced to its contract."""
    try:
        ratespec.validate(spec)
    except ratespec.SpecError as exc:
        return exc.problems

    problems = []
    if carrier is not None and spec["carrier"] != carrier:
        problems.append(f"carrier is {spec['carrier']!r}; this assignment is {carrier!r}")
    if spec["contract_file"] != index["file"]:
        problems.append(f"contract_file is {spec['contract_file']!r}; the contract is {index['file']!r}")
    if spec["agreement_ref"] != index["agreement_ref"]:
        problems.append(f"agreement_ref is {spec['agreement_ref']!r}; the contract header says {index['agreement_ref']!r}")
    term = index.get("term")
    if term is None:
        problems.append("the contract header states no term, so the spec's term cannot be traced")
    elif (spec["term"]["start"], spec["term"]["end"]) != (term["start"], term["end"]):
        problems.append(f"term is {spec['term']['start']}..{spec['term']['end']}; the contract header says "
                        f"{term['start']}..{term['end']} ({term['text']})")
    if "header" not in spec["term"]["clauses"]:
        problems.append("term must cite \"header\", where the contract states its term")

    clause_numbers = {c["id"]: set(c["numbers"]) for c in index["clauses"]}
    clause_numbers["header"] = _header_numbers(index)
    cited = ratespec.cited_clauses(spec)
    for element, ids in cited.items():
        unknown = [c for c in ids if c not in clause_numbers]
        if unknown:
            problems.append(f"{element} cites clauses that do not exist: {unknown}")

    for element, ids, values in priced_numbers(spec):
        available = set().union(*(clause_numbers.get(c, set()) for c in ids))
        for label, value in values:
            if canonical(value) not in available:
                problems.append(f"{element}: {label} {canonical(value)} does not appear in its cited clauses {ids}")

    used = {c for ids in cited.values() for c in ids}
    for clause in index["clauses"]:
        if clause["id"] not in used:
            problems.append(f"clause {clause['id']} is not cited anywhere; cite it where it is used, or list it under "
                            "gaps, non_pricing or unrepresentable")

    if vocabulary is not None:
        values: list[tuple[str, str]] = []
        for c in spec["components"]:
            if "when" in c:
                _condition_values(c["when"], values)
        values += [("service_level", s) for s in spec["service_levels"]["allowed"]]
        for kind, value in values:
            if value not in vocabulary[kind]:
                problems.append(f"{kind} value {value!r} does not occur in the shipment records "
                                f"(known: {vocabulary[kind]})")

    try:
        if date.fromisoformat(spec["term"]["start"]) > date.fromisoformat(spec["term"]["end"]):
            problems.append("term: start is after end")
    except ValueError as exc:
        problems.append(f"term: {exc}")
    return problems
