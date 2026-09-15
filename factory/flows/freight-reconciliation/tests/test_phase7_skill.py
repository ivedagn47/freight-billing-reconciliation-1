"""Phase 7: the reconcile-freight skill may only use real flowstate commands and flags, and may only name
nodes, gates, variables and artefact paths that the freight-reconciliation flow actually has."""

import argparse
import re

import yaml

import stage_flows
from flowstate import cli, runtime
from flowstate.loader import load_flow

SKILL = stage_flows.REPO / ".claude" / "skills" / "reconcile-freight" / "SKILL.md"
FLOW = stage_flows.FLOW_DIR / "freight-reconciliation.dot"
TEXT = SKILL.read_text()


def flow():
    return load_flow(FLOW, FLOW.with_suffix(".flow.yml"))


def test_frontmatter_and_the_orchestrator_skill_it_builds_on():
    meta = yaml.safe_load(TEXT.split("---")[1])
    assert meta["name"] == "reconcile-freight" and "/reconcile-freight 2026-07" in meta["description"]
    assert "**graph-orchestrator**" in TEXT and "Skill tool" in TEXT
    assert (stage_flows.REPO / ".claude" / "skills" / "graph-orchestrator" / "SKILL.md").is_file()


def test_every_flowstate_command_and_flag_exists():
    parser = cli.build_parser()
    action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    options = {name: {o for a in sub._actions for o in a.option_strings} for name, sub in action.choices.items()}
    spans = re.findall(r"`([^`]*flowstate [^`]*)`", TEXT)
    assert len(spans) >= 3
    for span in spans:
        verb = span.split("flowstate ", 1)[1].split()[0]
        assert verb in options, span
        for flag in re.findall(r"(?<![\w-])--[a-z][a-z-]*", span):
            assert flag in options[verb], span


def test_named_nodes_gates_situations_and_variables_exist():
    f = flow()
    table = TEXT.split("## What this flow's situations mean")[1].split("## Never")[0]
    named_nodes = {n for cell in re.findall(r"^\| ([^|]+) \|", table, re.M) for n in re.findall(r"`([a-z_]+)`", cell)}
    assert named_nodes and named_nodes <= set(f.nodes)
    agent_nodes = {n.id for n in f.nodes.values() if n.kind == "agent"}
    assert agent_nodes - {"extract_rules_rerun"} <= named_nodes | {"extract_rules"}

    gates_in_flow = {g.relative_to(stage_flows.FLOW_DIR).as_posix() for e in f.edges for g in e.gates}
    assert set(re.findall(r"`(gates/[a-z-]+\.sh)`", TEXT)) == gates_in_flow

    situations = set(re.findall(r"`([a-z_]+)`", table)) & set(runtime.OPTIONS)
    assert situations == {"gate_failed", "script_failed", "flow_changed"}

    for name in re.findall(r"--var ([a-z_]+)=", TEXT) + re.findall(r"`([a-z_]+)=", TEXT):
        assert name in f.input_vars, name
    assert "publication" in f.variables

    outputs = {file.path for s in f.output_schemas.values() for file in s.files}
    for path in re.findall(r"`(artefacts/[a-z0-9/._-]+)`", TEXT):
        assert "{_run_artefact_dir}/" + path.removeprefix("artefacts/") in outputs, path


def test_the_skill_keeps_the_orchestrator_out_of_the_reconciliation():
    never = TEXT.split("## Never")[1].split("## ")[0]
    for phrase in ("disposition", "amount", "memo", "reconciliation-report.json", "factory/flows/freight-reconciliation/",
                   "use_rules_cache"):
        assert phrase in never, phrase
