import copy
from decimal import Decimal

import pytest

from freight import ratespec
from freight.vocab import CHARGE_CODES, SHIPMENT_FIELDS

from synthetic import ACME_SPEC, spec


def test_synthetic_spec_is_valid():
    assert ratespec.validate(spec())["carrier"] == "acme"


def test_schema_vocabularies_match_the_code():
    s = ratespec.schema()
    codes = s["$defs"]["component"]["properties"]["charge_codes"]["items"]["enum"]
    assert set(codes) == set(CHARGE_CODES) - {"credit", "other"}
    fields = s["$defs"]["value_ref"]["oneOf"][0]["properties"]["field"]["enum"]
    assert set(fields) == {k for k, t in SHIPMENT_FIELDS.items() if t == "number"}


def mutate(fn):
    data = copy.deepcopy(ACME_SPEC)
    fn(data)
    return data


INVALID = [
    ("unknown calc op", lambda d: d["components"][2]["calc"].update(op="tiered"), "/components/2/calc"),
    ("unknown shipment field", lambda d: d["quantities"][0]["args"][0].update(field="volume_m3"), "/quantities/0"),
    ("charge code outside vocabulary", lambda d: d["components"][2]["charge_codes"].append("other"), "/components/2"),
    ("clause id is not a clause number", lambda d: d["components"][0]["clauses"].append("2a"), "/components/0/clauses"),
    ("missing required key", lambda d: d.pop("service_levels"), "service_levels"),
    ("discount of zero percent", lambda d: d["invoice_adjustments"][0]["calc"].update(percent=0), "/invoice_adjustments"),
    ("unknown condition", lambda d: d["components"][2].update(when={"weekday": "monday"}), "/components/2/when"),
    ("percent_of a later component", lambda d: d["components"].insert(0, {
        "name": "early", "kind": "surcharge", "charge_codes": ["fuel_surcharge"], "clauses": ["3"],
        "calc": {"op": "percent_of", "percent": 1, "of": ["freight"]}}), "not an earlier component"),
    ("quantity used before it is defined", lambda d: d["quantities"].insert(0, {
        "name": "double", "op": "max", "args": [{"quantity": "chargeable_kg"}, {"const": 1}], "clauses": ["1"]}),
     "not defined before"),
    ("duplicate names", lambda d: d["components"][2].update(name="fuel"), "used more than once"),
    ("overlapping inclusive bands", lambda d: [d["components"][0]["calc"]["bands"][0].update(max_inclusive=True),
                                               d["components"][0]["calc"]["bands"][1].update(min_inclusive=True)],
     "overlap at 100"),
    ("open-ended band not last", lambda d: d["components"][0]["calc"]["bands"][0].update(max=None), "overlap"),
    ("empty band", lambda d: d["components"][0]["calc"]["bands"][1].update(min=600), "empty"),
    ("term ends before it starts", lambda d: d["term"].update(end="2025-01-01"), "start is after end"),
]


@pytest.mark.parametrize("label,fn,message", INVALID, ids=[i[0] for i in INVALID])
def test_invalid_specs_are_rejected(label, fn, message):
    with pytest.raises(ratespec.SpecError) as exc:
        ratespec.validate(mutate(fn))
    assert any(message in p for p in exc.value.problems), exc.value.problems


@pytest.mark.parametrize("value,band,inside", [
    ("100", {"min": None, "min_inclusive": False, "max": 100, "max_inclusive": False}, False),
    ("99.99", {"min": None, "min_inclusive": False, "max": 100, "max_inclusive": False}, True),
    ("100", {"min": 100, "min_inclusive": True, "max": 500, "max_inclusive": True}, True),
    ("500", {"min": 100, "min_inclusive": False, "max": 500, "max_inclusive": True}, True),
    ("500.01", {"min": 100, "min_inclusive": False, "max": 500, "max_inclusive": True}, False),
    ("0", {"min": None, "min_inclusive": False, "max": None, "max_inclusive": False}, True),
])
def test_band_membership_follows_the_written_ends(value, band, inside):
    assert ratespec.in_band(Decimal(value), {**band, "rate": 1}) is inside


def test_pricing_view_ignores_citations_and_wording_but_not_numbers():
    a = spec()
    b = spec()
    b["components"][0]["clauses"] = ["7"]
    b["components"][0]["description"] = "worded differently"
    b["components"][0]["calc"]["bands"][1]["rate"] = 1.50
    b["gaps"] = []
    b["service_levels"]["allowed"] = ["standard"]
    assert ratespec.pricing_view(a) == ratespec.pricing_view(b)
    c = spec()
    c["components"][1]["calc"]["percent"] = 12
    assert ratespec.pricing_view(a) != ratespec.pricing_view(c)


def test_cited_clauses_lists_every_element():
    cited = ratespec.cited_clauses(spec())
    assert cited["term"] == ["header"] and cited["components[2].residential"] == ["4"]
    assert cited["invoice_adjustments[0].volume"] == ["5"] and cited["gaps[0]"] == ["2"]
