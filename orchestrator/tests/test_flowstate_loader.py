import pytest

from flowstate.errors import FlowstateError
from flowstate.loader import check_flow, load_flow
from flowstate.paths import FLOWS_DIR

from flowstate_helpers import SCRIPT_FLOW_DOT, SCRIPT_FLOW_FILES, SCRIPT_FLOW_YML, write_flow


def codes(dot_path):
    _, issues = check_flow(dot_path, dot_path.parent / f"{dot_path.stem}.flow.yml")
    return {i["code"] for i in issues.errors}, issues


def test_existing_smoke_test_flow_loads_unchanged():
    d = FLOWS_DIR / "smoke-test"
    flow = load_flow(d / "smoke-test.dot", d / "smoke-test.flow.yml")
    assert {n.id: n.kind for n in flow.nodes.values()} == {
        "start": "start", "research": "agent", "summarise": "agent", "done": "done"}
    research = flow.nodes["research"]
    assert research.prompt_template.name == "research.md" and research.working_dir == "{_run_artefact_dir}"
    assert (research.model, research.permission_mode, research.pause_at) == ("sonnet", "auto", "optional")
    assert [g.name for g in flow.edges[1].gates] == ["brief-exists.sh"]
    assert flow.input_vars == {"research_topic"}
    assert flow.output_schemas["research_outputs"].files[0].definition == "research-brief"


def test_existing_smoke_branch_is_valid_with_one_fork_region():
    d = FLOWS_DIR / "smoke-branch"
    flow = load_flow(d / "smoke-branch.dot", d / "smoke-branch.flow.yml")
    region = flow.regions["fork"]
    assert (region.kind, region.join, region.nodes) == ("fork", "j", {"worker_a", "worker_b"})
    assert region.branches == {"worker_a": {"worker_a"}, "worker_b": {"worker_b"}}
    assert region.produced == {"note_a", "note_b"}
    assert flow.nodes["j"].reducer_script.name == "reduce.sh" and flow.nodes["j"].summary_var == "branch_summary"


def test_script_flow_is_valid(tmp_path):
    found, issues = codes(write_flow(tmp_path, "s", SCRIPT_FLOW_DOT, SCRIPT_FLOW_YML, SCRIPT_FLOW_FILES))
    assert found == set(), issues


@pytest.mark.parametrize("mutate,expected", [
    (lambda d, y, f: (d.replace('start [shape=Mdiamond]', 'start [shape=Mdiamond]\n  s2 [shape=Mdiamond]\n  s2 -> make'), y, f), "start_count"),
    (lambda d, y, f: (d.replace("done  [shape=Msquare]", "done  [shape=box]"), y, f), "unknown_node_type"),
    (lambda d, y, f: (d.replace('scripts/make.sh', 'scripts/nope.sh'), y, f), "missing_file"),
    (lambda d, y, f: (d.replace('gates/check.sh', 'gates/nope.sh'), y, f), "missing_file"),
    (lambda d, y, f: (d, y.replace("definition: note", "definition: absent"), f), "missing_file"),
    (lambda d, y, f: (d.replace('output_schema="make_outputs"', 'output_schema="make_outputs", colour="red"'), y, f), "unsupported_attribute"),
    (lambda d, y, f: (d.replace('runner=script', 'runner=teleport'), y, f), "unknown_runner"),
    (lambda d, y, f: (d.replace('runner=script, script="scripts/make.sh"', 'runner=script, prompt_template="p.md"'), y, f), "unsupported_attribute"),
    (lambda d, y, f: (d.replace("make -> done", "make -> start"), y, f), "invalid_connectivity"),
    (lambda d, y, f: (d.replace("  start -> make\n", "  start -> done\n"), y, f), "unreachable_node"),
    (lambda d, y, f: (d + "\n", y, {**f, "scripts/make.sh": f["scripts/make.sh"].replace("greeting", "undeclared")}), "undeclared_variable"),
    (lambda d, y, f: (d.replace("make -> done [", 'make -> done [condition="count >", '), y, f), "invalid_condition"),
    (lambda d, y, f: (d.replace("start -> make", "start -> make\n  start -> done"), y, f), "ambiguous_branch"),
    (lambda d, y, f: (d.replace('working_dir="{_run_artefact_dir}"', 'working_dir="{note_path}"'), y, f), "variable_not_available"),
    (lambda d, y, f: (d, y.replace("      count: {file: note, pointer: /count}", "      count: {file: nope}"), f), "invalid_output_schema"),
    (lambda d, y, f: (d, y + "\nsurprise: 1\n", f), "unsupported_key"),
    (lambda d, y, f: (d.replace("digraph s {", "graph s {").replace("->", "--"), y, f), "dot_parse_error"),
    (lambda d, y, f: (d.replace("make -> done [", "make -> make\n  make -> done ["), y, f), "cycle"),
    (lambda d, y, f: (d, y, {**f, "gates/check.sh": "no shebang here\n"}), "not_executable"),
])
def test_static_validation_catches(tmp_path, mutate, expected):
    dot, yml, files = mutate(SCRIPT_FLOW_DOT, SCRIPT_FLOW_YML, dict(SCRIPT_FLOW_FILES))
    found, issues = codes(write_flow(tmp_path, "s", dot, yml, files))
    assert expected in found, issues


def test_agent_node_requires_prompt_and_outputs(tmp_path):
    dot = """
    digraph a {
      start [shape=Mdiamond]
      think [shape=box, prompt_template="prompts/p.md"]
      done  [shape=Msquare]
      start -> think
      think -> done
    }
    """
    found, _ = codes(write_flow(tmp_path, "a", dot, "variables: {}\n", {"prompts/p.md": "hi {missing_var}"}))
    assert {"missing_attribute", "undeclared_variable"} <= found


def test_load_flow_raises_one_error_with_all_issues(tmp_path):
    dot_path = write_flow(tmp_path, "s", SCRIPT_FLOW_DOT.replace("scripts/make.sh", "x.sh"), SCRIPT_FLOW_YML,
                          {"gates/check.sh": SCRIPT_FLOW_FILES["gates/check.sh"]})
    with pytest.raises(FlowstateError) as exc:
        load_flow(dot_path, dot_path.parent / "s.flow.yml")
    assert exc.value.code == "invalid_flow" and exc.value.details["issues"]
