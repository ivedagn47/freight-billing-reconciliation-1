"""Found by the Phase 6 live extraction run: contracts condition charges on facts the shipment records do not
hold, and extractions differ on charge codes the carrier never bills. Covered here: three-valued conditions
(`unrecorded`), the flags and policy they lead to, agreement on them, comparison restricted to the codes a
carrier's invoices can carry, and the parsers' declared codes."""

import copy
from pathlib import Path

import pytest

from freight import agreement, documents, policy, pricing, ratespec

from conftest import FLOW_DIR, REPO
from synthetic import invoice, line, shipment, spec
from test_phase6_checks import VOCAB

POLICY = policy.load(FLOW_DIR / "config" / "policy.yml")
UPPER_FLOOR = {"unrecorded": "the delivery is to an upper floor"}


def with_residential_when(when: dict) -> dict:
    data = spec()
    data["components"][2]["when"] = when
    return ratespec.validate(data)


EITHER = {"any": [{"special_handling_includes": "residential"}, UPPER_FLOOR]}


@pytest.mark.parametrize("when,handling,applied,unverifiable", [
    (EITHER, ["residential"], True, False),       # the recorded part settles it
    (EITHER, [], False, True),                    # only the unrecorded fact could make it apply
    ({"all": [{"special_handling_includes": "residential"}, UPPER_FLOOR]}, [], False, False),  # recorded part is false
    ({"all": [{"special_handling_includes": "residential"}, UPPER_FLOOR]}, ["residential"], False, True),
    ({"not": UPPER_FLOOR}, [], False, True),
])
def test_conditions_are_three_valued(when, handling, applied, unverifiable):
    result = pricing.evaluate(with_residential_when(when), shipment("AC-1", 50, special_handling=handling))
    row = result["components"][2]
    assert row["applied"] is applied and bool(row.get("unverifiable")) is unverifiable
    assert str(result["amount"]) == ("160.00" if applied else "110.00")  # an unverifiable component is not priced


def priced_line(charges, handling, s=None):
    s = s or with_residential_when(EITHER)
    priced = pricing.price([invoice("ACME-07", [line(1, "AC-1", charges)])], ["ACME-07"],
                           [shipment("AC-1", 50, special_handling=handling)], {"acme": s})
    return priced["lines"][0], policy.apply(priced, POLICY)["decisions"]["ACME-07#1"]


def test_a_charge_only_an_unrecorded_fact_could_justify_is_escalated():
    row, decision = priced_line([("freight_incl_fuel", "110.00"), ("residential_delivery", "50.00")], [])
    assert [f["code"] for f in row["flags"]] == ["CHARGE_UNVERIFIABLE"]
    assert row["flags"][0]["details"]["unrecorded"] == ["the delivery is to an upper floor"]
    assert (row["expected_amount"], row["delta"], decision["disposition"]) == ("110.00", "50.00", "escalate")

    row, decision = priced_line([("freight_incl_fuel", "110.00")], [])  # not billed: nothing to verify
    assert row["flags"] == [] and decision["disposition"] == "accept"
    row, decision = priced_line([("freight_incl_fuel", "110.00"), ("residential_delivery", "50.00")], ["residential"])
    assert row["flags"] == [] and decision["disposition"] == "accept"


def test_a_percentage_of_an_unverifiable_component_is_undetermined():
    data = spec()
    data["components"] = [data["components"][0], {**data["components"][2], "when": EITHER}, data["components"][1]]
    data["components"][2]["calc"] = {"op": "percent_of", "percent": 10, "of": ["freight", "residential"]}
    row, decision = priced_line([("freight_incl_fuel", "115.00")], [], ratespec.validate(data))
    assert row["expected_amount"] is None and "CONDITION_UNVERIFIABLE" in [f["code"] for f in row["flags"]]
    assert decision["disposition"] == "escalate"


def test_agreement_catches_a_copy_that_drops_the_unrecorded_part():
    kept, dropped = with_residential_when(EITHER), spec()
    result = agreement.compare(kept, dropped, VOCAB, ["freight", "residential_delivery"])
    assert not result["agree"]
    assert any(d["a"].get("charge_codes", {}).get("residential_delivery") == "unverifiable" for d in result["differences"])
    assert agreement.compare(kept, copy.deepcopy(kept), VOCAB)["agree"]


def test_agreement_compares_only_codes_the_carrier_bills():
    a, b = spec(), spec()
    b["components"][2]["charge_codes"].append("handling")  # the carriers' readings differ on handling fees
    assert agreement.compare(a, b, VOCAB, ["freight", "freight_incl_fuel", "residential_delivery"])["agree"]
    result = agreement.compare(a, b, VOCAB, ["freight", "handling"])
    assert not result["agree"] and "handling" in result["differences"][0]["a"]["charge_codes"]


def test_parsers_declare_every_code_they_emit_on_the_real_invoices():
    carriers = documents.load_carriers(FLOW_DIR / "config" / "carriers.yml")
    for doc in documents.discover(REPO / "data" / "invoices", carriers):
        emitted = {c["code"] for l in doc["lines"] for c in l["charges"]}
        from freight import parsers
        assert emitted <= parsers.FORMATS[doc["format"]].CHARGE_CODES, (doc["doc_id"], emitted)
    assert documents.invoice_charge_codes(carriers, "alpine") == ["freight", "handling"]
    assert documents.invoice_charge_codes(carriers, "sagar") == ["cold_chain_premium", "freight"]
    assert "credit" not in documents.invoice_charge_codes(carriers, "falcon")
