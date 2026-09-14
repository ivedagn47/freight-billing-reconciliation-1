from decimal import Decimal
from pathlib import Path

import pytest

from freight import contracts
from freight.money import fmt, round_inr, to_decimal, to_number, within

from conftest import REPO


@pytest.mark.parametrize("raw,expected", [
    ("Rs 17,472.00", Decimal("17472.00")), ("Rs -496.80", Decimal("-496.80")), ("₹9.50", Decimal("9.50")),
    (" 2,000 ", Decimal("2000")), (589.875, Decimal("589.875")), (12, Decimal(12)), ("0.1", Decimal("0.1")),
])
def test_to_decimal(raw, expected):
    assert to_decimal(raw) == expected


@pytest.mark.parametrize("raw", ["", "Rs", "12a", "1.2.3", True, None, [1]])
def test_to_decimal_rejects_non_amounts(raw):
    with pytest.raises(ValueError):
        to_decimal(raw)


def test_rounding_is_half_up_to_the_paisa():
    assert fmt("589.875") == "589.88" and fmt("0.005") == "0.01" and fmt("-0.005") == "-0.01"
    assert round_inr(0.1 + 0.2) == Decimal("0.30") and to_number("17472") == 17472.0
    assert within("10.00", "10.01", "0.01") and not within("10.00", "10.02", "0.01")


SYNTHETIC_CONTRACT = """\
# Services Agreement — Example Carrier

**Agreement ref:** EX/1
**Between:** A and B
**Term:** 1 January 2030 – 31 December 2031

## Charges

1. Freight is **₹2.50** per kg,
   with a minimum of 10 kg.
2. Rates:
   - under 1,000 kg: ₹3.00
   - over 1,000 kg: ₹2.75

## Other

3. A 5% discount applies above 12 consignments.
"""


def test_clause_index_of_a_synthetic_contract(tmp_path):
    path = tmp_path / "example.md"
    path.write_text(SYNTHETIC_CONTRACT)
    ix = contracts.index(path)
    assert (ix["title"], ix["agreement_ref"], ix["parties"]) == ("Services Agreement — Example Carrier", "EX/1", "A and B")
    assert ix["term"] == {"start": "2030-01-01", "end": "2031-12-31", "text": "1 January 2030 – 31 December 2031"}
    assert [(c["id"], c["section"]) for c in ix["clauses"]] == [("1", "Charges"), ("2", "Charges"), ("3", "Other")]
    assert ix["clauses"][0]["text"] == "Freight is ₹2.50 per kg, with a minimum of 10 kg."
    assert ix["clauses"][1]["text"] == "Rates: - under 1,000 kg: ₹3.00 - over 1,000 kg: ₹2.75"
    assert ix["clauses"][1]["numbers"] == ["1000", "3", "2.75"]
    assert ix["clauses"][2]["numbers"] == ["5", "12"]
    assert contracts.clause(ix, "3")["section"] == "Other" and contracts.clause(ix, "9") is None


@pytest.mark.parametrize("mutation,message", [
    (lambda t: t.replace("1. Freight", "Freight").replace("2. Rates", "Rates").replace("3. A 5%", "A 5%"), "no numbered clauses"),
    (lambda t: t.replace("3. A 5%", "2. A 5%"), "duplicate clause"),
    (lambda t: t.replace("31 December 2031", "the end"), "start and end date"),
    (lambda t: t.replace("1 January 2030", "1 Janvier 2030"), "unrecognised month"),
])
def test_clause_index_rejects_malformed_contracts(tmp_path, mutation, message):
    path = tmp_path / "bad.md"
    path.write_text(mutation(SYNTHETIC_CONTRACT))
    with pytest.raises(contracts.ContractError, match=message):
        contracts.index(path)


def test_real_contracts_index_structurally():
    expected = {"alpine-express.md": ("AEL/BFC/2025-03", 7), "falcon-freight.md": ("FF/BFC/2024-11", 8),
                "sagar-roadlines.md": ("SRL/BFC/2025-08", 6)}
    for name, (ref, count) in expected.items():
        ix = contracts.index(REPO / "data" / "contracts" / name)
        assert ix["agreement_ref"] == ref
        assert [c["id"] for c in ix["clauses"]] == [str(i) for i in range(1, count + 1)]
        assert ix["term"]["start"] < ix["term"]["end"] and all(c["text"] and c["section"] for c in ix["clauses"])
