"""Phase 6 extraction bookkeeping: assignments, agreement rounds, adoption and the rate-spec cache."""

import json
import shutil

import pytest

from freight import documents, rules

from conftest import FLOW_DIR
from stage_flows import acme_inputs
from synthetic import spec


def boundary_variant() -> dict:
    """Traces to the contract, but reads "under 100 kg" as "up to 100 kg"."""
    data = spec()
    data["components"][0]["calc"]["bands"][0]["max_inclusive"] = True
    return data


@pytest.fixture
def env(tmp_path):
    inputs = acme_inputs(tmp_path / "inputs")
    carriers = documents.load_carriers(inputs["carriers"])

    def plan(use_cache=True, flow_dir=FLOW_DIR):
        return rules.plan(carriers, ["acme"], inputs["root"], inputs["shipment_records"], tmp_path / "work",
                          tmp_path / "cache", use_cache, flow_dir)

    def copy(name, data, session):
        path = tmp_path / "copies" / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**data, "_session_id": session}))
        return str(path)

    return {"tmp": tmp_path, "plan": plan, "copy": copy, "inputs": inputs, "out": tmp_path / "work"}


def test_a_cache_miss_assigns_two_independent_copies(env):
    plan = env["plan"]()
    assert plan["carriers"]["acme"]["cache"] == "miss"
    items = plan["extraction_items"]
    assert [(i["carrier"], i["copy"], i["round"]) for i in items] == [("acme", "a", 1), ("acme", "b", 1)]
    assert items[0]["inputs"] == [str(env["inputs"]["contract"].resolve()), plan["carriers"]["acme"]["clause_index"]]
    assert items[0]["vocabulary"] == {"service_level": ["express", "standard"], "special_handling": ["fragile", "residential"]}
    assert items[0]["invoice_charge_codes"] == ["freight", "handling"]  # what the acme config's alpine-json format emits
    assert json.loads((env["out"] / "clauses" / "acme.json").read_text())["agreement_ref"] == "ACME/1"
    assert rules.plan(documents.load_carriers(env["inputs"]["carriers"]), ["acme"], env["inputs"]["root"], [],
                      env["tmp"] / "w2", None, False)["carriers"]["acme"]["cache"] == "disabled"


def test_agreed_copies_are_adopted_cached_and_reused(env):
    plan = env["plan"]()
    record, blocking = rules.agree(plan, [env["copy"]("a", spec(), "s-a"), env["copy"]("b", spec(), "s-b")], env["out"])
    assert blocking == {} and record["carriers"]["acme"]["status"] == "agreed" and record["rerun_items"] == []
    final, blocking = rules.final(plan, record, [], env["out"], run_id="run-1")
    assert blocking == {} and final["sources"]["acme"]["source"] == "round 1"
    entry = json.loads(open(final["cache_writes"][0]).read())
    assert entry["key_parts"]["contract_sha256"] == plan["carriers"]["acme"]["contract_sha256"]
    assert [c["session_id"] for c in entry["agreement"]["copies"]] == ["s-a", "s-b"] and entry["agreement"]["run_id"] == "run-1"

    again = env["plan"]()
    assert again["carriers"]["acme"]["cache"] == "hit" and again["extraction_items"] == []
    record, _ = rules.agree(again, [], env["out"])
    final, blocking = rules.final(again, record, [], env["out"])
    assert blocking == {} and final["sources"]["acme"]["source"] == "cache" and final["cache_writes"] == []
    assert json.loads(open(final["specs"]["acme"]).read())["components"] == spec()["components"]


def test_the_cache_key_follows_the_prompt_and_stale_entries_are_ignored(env):
    plan = env["plan"]()
    record, _ = rules.agree(plan, [env["copy"]("a", spec(), "s-a"), env["copy"]("b", spec(), "s-b")], env["out"])
    rules.final(plan, record, [], env["out"])

    edited = env["tmp"] / "edited-flow"
    for name in ("prompts", "definitions"):
        shutil.copytree(FLOW_DIR / name, edited / name)
    guide = edited / "prompts" / "reference" / "rate-spec-guide.md"
    guide.write_text(guide.read_text() + "\nAn extra instruction.\n")
    changed = env["plan"](flow_dir=edited)
    assert changed["prompt_version"] != plan["prompt_version"] and changed["carriers"]["acme"]["cache"] == "miss"

    entry_path = plan["carriers"]["acme"]["cache_entry"]
    entry = json.loads(open(entry_path).read())
    entry["spec"]["components"][2]["calc"]["amount"] = 55
    open(entry_path, "w").write(json.dumps(entry))
    stale = env["plan"]()
    assert stale["carriers"]["acme"]["cache"] == "invalid" and len(stale["extraction_items"]) == 2
    assert "amount 55 does not appear" in stale["carriers"]["acme"]["cache_problems"][0]


def test_disagreement_gets_one_fresh_round_that_must_agree_on_its_own(env):
    plan = env["plan"]()
    record, blocking = rules.agree(plan, [env["copy"]("a", spec(), "s-a"), env["copy"]("b", boundary_variant(), "s-b")],
                                   env["out"])
    acme = record["carriers"]["acme"]
    assert blocking == {} and acme["status"] == "disagreed" and acme["comparison"]["differences"]
    assert [(i["copy"], i["round"]) for i in record["rerun_items"]] == [("c", 2), ("d", 2)]

    final, blocking = rules.final(plan, record, [env["copy"]("c", spec(), "s-c"), env["copy"]("d", spec(), "s-d")],
                                  env["out"])
    assert blocking == {} and final["sources"]["acme"]["source"] == "round 2"
    assert final["sources"]["acme"]["matches_round1_copies"] == ["a"]

    final, blocking = rules.final(plan, record, [env["copy"]("c2", spec(), "s-c2"),
                                                 env["copy"]("d2", boundary_variant(), "s-d2")], env["out"])
    assert blocking == {"acme": "disagreed"} and "acme" not in final["specs"] and final["cache_writes"] == []


@pytest.mark.parametrize("b,session,status", [
    ({**spec(), "components": [spec()["components"][0]]}, "s-b", "invalid"),
    ({**spec(), "unrepresentable": [{"description": "a per-hour waiting charge", "clauses": ["4"]}]}, "s-b", "unrepresentable"),
    (spec(), "s-a", "invalid"),
])
def test_invalid_duplicated_or_unrepresentable_copies_stop_the_run(env, b, session, status):
    plan = env["plan"]()
    record, blocking = rules.agree(plan, [env["copy"]("a", spec(), "s-a"), env["copy"]("b", b, session)], env["out"])
    assert blocking == {"acme": status} and record["rerun_items"] == []
    final, blocking = rules.final(plan, record, [], env["out"])
    assert blocking == {"acme": status} and final["specs"] == {}


def test_mismatched_inputs_are_refused(env):
    plan = env["plan"]()
    with pytest.raises(rules.RulesError):
        rules.agree(plan, [env["copy"]("a", spec(), "s-a")], env["out"])
    with pytest.raises(rules.RulesError):
        rules.plan(documents.load_carriers(env["inputs"]["carriers"]), ["nobody"], env["inputs"]["root"], [],
                   env["out"], None, False)
    with pytest.raises(rules.RulesError, match="absolute path"):  # found live: a relative path missed the cache
        rules.plan(documents.load_carriers(env["inputs"]["carriers"]), ["acme"], env["inputs"]["root"], [],
                   env["out"], rules.Path("runs/cache"), True)
    manifest = {"carriers": {"acme": {"in_scope": ["ACME-07"]}, "other": {"in_scope": []}}}
    assert rules.carriers_in_scope(manifest) == ["acme"]


def test_a_cached_spec_is_reused_only_for_the_same_extraction_inputs(env):
    plan = env["plan"]()
    record, _ = rules.agree(plan, [env["copy"]("a", spec(), "s-a"), env["copy"]("b", spec(), "s-b")], env["out"])
    rules.final(plan, record, [], env["out"])
    carriers_file = env["inputs"]["carriers"]
    carriers_file.write_text(carriers_file.read_text().replace("[alpine-json]", "[alpine-json, sagar-csv]"))
    changed = rules.plan(documents.load_carriers(carriers_file), ["acme"], env["inputs"]["root"],
                         env["inputs"]["shipment_records"], env["out"], env["tmp"] / "cache", True)
    acme = changed["carriers"]["acme"]
    assert acme["cache"] == "invalid" and "different shipment vocabulary or invoice charge codes" in acme["cache_problems"][0]
    assert acme["invoice_charge_codes"] == ["cold_chain_premium", "freight", "handling"]
