"""Phase 4: the graph-orchestrator skill (.claude/skills/graph-orchestrator/).

Token-free, two layers:
1. Contract. SKILL.md and situations.md may only use commands, flags, situations and error codes
   the real flowstate CLI and runtime have, and must document every situation the runtime returns.
2. Protocol. `Supervisor` is a test double for the orchestrator agent: it applies SKILL.md's decision
   procedure literally, uses only `orchestrator/bin/flowstate` in subprocesses plus read-only file
   access, and supervises fake-worker runs. It proves the documented command sequences produce the
   documented outcomes against the real runtime. A real model applying the skill is exercised by
   orchestrator/tests/live/orchestrate_drill.sh.
"""

import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path

import jsonschema
import pytest
import yaml

from agentctl import lifecycle
from agentctl.registry import Registry
from flowstate import cli, runtime
from flowstate.outputs import json_pointer

from conftest import LIB, requires_tmux

REPO = LIB.parents[1]
SKILL_DIR = REPO / ".claude" / "skills" / "graph-orchestrator"
SKILL = SKILL_DIR / "SKILL.md"
SITUATIONS = SKILL_DIR / "situations.md"
FLOWSTATE = REPO / "orchestrator" / "bin" / "flowstate"
DRILL = REPO / "orchestrator" / "tests" / "fixtures" / "flows" / "orchestrator-drill"
DRILL_FAKES = REPO / "orchestrator" / "tests" / "fixtures" / "fake" / "orchestrator-drill"
ALLOWED_VERBS = {"validate", "init", "advance", "status", "events", "retry", "respawn", "pause", "resume", "abort"}


# ================================================================ 1. contract

def _subcommand_options() -> dict[str, set[str]]:
    parser = cli.build_parser()
    action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return {name: {opt for a in sub._actions for opt in a.option_strings} for name, sub in action.choices.items()}


def _code_spans(text: str) -> list[str]:
    fences = re.findall(r"```.*?```", text, re.S)
    rest = re.sub(r"```.*?```", "", text, flags=re.S)
    return fences + re.findall(r"`([^`]+)`", rest)


def test_skill_has_frontmatter_and_links_its_reference():
    text = SKILL.read_text()
    assert text.startswith("---\n")
    meta = yaml.safe_load(text.split("---")[1])
    assert meta["name"] == "graph-orchestrator"
    assert "flowstate" in meta["description"] and len(meta["description"]) > 80
    assert "(situations.md)" in text and SITUATIONS.is_file()


def test_every_documented_command_and_flag_exists():
    options = _subcommand_options()
    checked = 0
    for doc in (SKILL, SITUATIONS):
        for span in _code_spans(doc.read_text()):
            for segment in re.split(r"(?=\bflowstate )", span)[1:]:
                verb = segment.split()[1]
                assert verb in options, f"{doc.name}: `flowstate {verb}` is not a real subcommand"
                for flag in re.findall(r"(?<![\w-])--[a-z][a-z-]*", segment):
                    assert flag in options[verb], f"{doc.name}: `flowstate {verb}` has no {flag}"
                checked += 1
    assert checked >= 15


def _documented_situations() -> set[str]:
    section = SITUATIONS.read_text().split("## Command errors")[0]
    return set(re.findall(r"^\| `([a-z_]+)` \|", section, re.M))


def test_situation_reference_matches_the_runtime_exactly():
    assert _documented_situations() == set(runtime.OPTIONS)


def test_skill_decision_procedure_mentions_every_situation():
    text = SKILL.read_text()
    missing = [s for s in runtime.OPTIONS if f"`{s}`" not in text]
    assert missing == []


def test_documented_error_codes_are_raised_by_the_runtime():
    source = "\n".join(p.read_text() for p in (LIB / "flowstate").glob("*.py"))
    raised = set(re.findall(r'FlowstateError\(\s*"([a-z_]+)"', source))
    section = SITUATIONS.read_text().split("## Command errors")[1]
    documented = {code for row in re.findall(r"^\| (.+?) \|", section, re.M) for code in re.findall(r"`([a-z_]+)`", row)}
    assert documented and documented <= raised, documented - raised


def test_skill_forbids_bypassing_flowstate():
    never = SKILL.read_text().split("## Never")[1].split("## ")[0]
    for phrase in ("state.yaml", "tmux", "agentctl spawn", "fabricate", "flow's DOT", "retry counter",
                   "Abort on your own initiative"):
        assert phrase in never, phrase


# ================================================================ 2. protocol

class Supervisor:
    """Applies SKILL.md's decision procedure. Uses only the flowstate CLI and read-only file access."""

    WAITING = {"worker_running", "script_running", "branches_running"}
    NEEDS_HUMAN = {"render_failed", "condition_error", "no_route", "ambiguous_route", "fanout_invalid",
                   "flow_changed", "retries_exhausted"}
    DETERMINISTIC = {"script_failed", "reducer_failed", "reducer_output_invalid"}
    INTERRUPTED = {"node_interrupted", "reducer_interrupted"}

    def __init__(self, runs: str, run_id: str, max_wait: float = 30, on_decision=None):
        self.runs, self.run_id, self.max_wait, self.on_decision = runs, run_id, max_wait, on_decision
        self.commands: list[list[str]] = []
        self.decisions: list[tuple] = []
        self._retried: set = set()
        self._waited: set = set()

    def flowstate(self, verb: str, *args: str) -> tuple[dict, int]:
        assert verb in ALLOWED_VERBS
        proc = subprocess.run([str(FLOWSTATE), verb, "--runs-dir", self.runs, *args],
                              capture_output=True, text=True, timeout=240)
        self.commands.append([verb, *args])
        return json.loads(proc.stdout), proc.returncode

    def advance(self, *extra: str) -> dict:
        return self.flowstate("advance", self.run_id, "--max-wait", str(self.max_wait), *extra)[0]

    def supervise(self, limit: int = 25) -> dict:
        sit = None
        for _ in range(limit):
            sit = sit or self.advance()
            kind = sit["situation"]
            if kind in ("completed", "aborted", "paused"):
                return sit
            if kind in self.WAITING:
                sit = None
                continue
            decision = self.decide(sit)
            self.decisions.append((kind, sit.get("node"), sit.get("branch_id"), decision[0]))
            if self.on_decision:
                self.on_decision(sit, decision)
            sit = self.act(sit, decision)
        raise AssertionError(f"supervision did not terminate: {self.decisions}")

    def decide(self, sit: dict) -> tuple[str, object]:
        kind, key = sit["situation"], (sit.get("node"), sit.get("branch_id"))
        if kind in self.NEEDS_HUMAN or sit.get("retries_remaining") == 0:
            return "pause", f"{kind} at {sit.get('node')}: automatic recovery is not possible"
        if kind == "validation_failed":
            if key in self._retried:
                return "respawn", "the same output problem recurred after precise feedback"
            return "retry", self.validation_feedback(sit)
        if kind == "gate_failed":
            if sit["node_kind"] != "agent":
                return "pause", "a gate failed on deterministic script output"
            return "retry", (f"Check {sit['gate']} failed (exit {sit['exit_code']}): {sit.get('stderr_tail')}. "
                             "Rewrite the output so it satisfies that check; keep every other field.")
        if kind == "worker_stalled":
            if key in self._waited:
                return "respawn", "stalled again after waiting with a longer threshold"
            return "wait", sit["stall_after_s"] * 2
        if kind == "worker_timeout":
            return "respawn", "node timeout exceeded"
        if kind == "worker_failed":
            if key in self._retried or sit.get("worker_state") in ("launch_failed", "send_failed"):
                return "pause", f"worker failed ({sit.get('worker_state')}) and a retry cannot help"
            return "retry", f"Your previous run ended as {sit['worker_state']}; continue the task."
        if kind in self.DETERMINISTIC:
            return "pause", f"{kind}: deterministic code failed and the evidence shows no transient cause"
        if kind in self.INTERRUPTED:
            return ("pause", "interrupted twice") if key in self._retried else ("retry", None)
        return "pause", f"unrecognised situation {kind}"

    def validation_feedback(self, sit: dict) -> str:
        lines = []
        rejected = {Path(p).name: Path(p) for p in (sit.get("evidence") or {}).get("rejected_outputs", [])}
        for e in sit["errors"]:
            name = Path(e["path"]).name
            received = ""
            if e["error"] == "schema" and name in rejected:  # read the evidence, never edit it
                doc = json.loads(rejected[name].read_text())
                received = f" (received {json.dumps(json_pointer(doc, e['at']))})"
            lines.append(f"Output failed schema validation at {e.get('at', '/')} in {name}: {e['message']}{received}.")
        lines.append("Rewrite the file at the same path so it satisfies the schema; keep every other field.")
        return " ".join(lines)

    def act(self, sit: dict, decision: tuple[str, object]) -> dict | None:
        verb, arg = decision
        node, key = sit.get("node"), (sit.get("node"), sit.get("branch_id"))
        branch = ["--branch", sit["branch_id"]] if sit.get("branch_id") and sit.get("node_kind") != "join" else []
        if verb == "retry":
            self._retried.add(key)
            out, code = self.flowstate("retry", self.run_id, node, *branch, *(["--feedback", arg] if arg else []))
        elif verb == "respawn":
            out, code = self.flowstate("respawn", self.run_id, node, *branch, "--reason", arg)
        elif verb == "wait":
            self._waited.add(key)
            return self.advance("--stall-after", str(arg))
        else:
            out, code = self.flowstate("pause", self.run_id, "--reason", arg)
        assert code == 0, out
        return out if "situation" in out else None


@pytest.fixture
def prefs(tmp_path, monkeypatch):
    def write(**values):
        path = tmp_path / "prefs.yml"
        path.write_text(yaml.safe_dump({"supervision": "low", "max_retries": 2, **values}))
        monkeypatch.setenv("FLOWSTATE_PREFS", str(path))
    write()
    monkeypatch.delenv("DRILL_FINISH_FAIL", raising=False)
    return write


@pytest.fixture
def runs(tmp_path):
    return str(tmp_path / "runs")


@pytest.fixture
def cleanup_workers(runs):
    yield
    for run_dir in Path(runs).glob("*"):
        reg = Registry(run_dir / "workers")
        for worker in reg.worker_ids():
            if lifecycle.status(reg, worker)["state"] in ("running", "stalled"):
                lifecycle.kill(reg, worker)


def init_drill(runs: str, plan: Path = DRILL_FAKES / "plan.json", work: Path = DRILL_FAKES / "work.json",
               flow: Path = DRILL, run_id: str = "d1") -> str:
    proc = subprocess.run([str(FLOWSTATE), "init", str(flow), "--runs-dir", runs, "--run-id", run_id,
                           "--harness", "fake", "--fake-script", f"plan={plan}", "--fake-script", f"work={work}"],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout
    return run_id


def state_of(runs: str, run_id: str = "d1") -> dict:
    return yaml.safe_load((Path(runs) / run_id / "state.yaml").read_text())


def events_of(runs: str, run_id: str = "d1") -> list[dict]:
    return [json.loads(l) for l in (Path(runs) / run_id / "events.jsonl").read_text().splitlines()]


def schema(name: str) -> dict:
    return json.loads((DRILL / "definitions" / f"{name}.json").read_text())


def always_one_step() -> dict:
    return {"invocations": [{"steps": [
        {"capture": {"name": "out", "regex": "(\\S+/plan\\.json)"}},
        {"write": {"path": "${out}", "json": {"_session_id": "${session_id}", "goal": "g", "steps": ["only-one"]}}}]}]}


@requires_tmux
def test_supervisor_completes_the_drill_through_two_retries(prefs, runs, cleanup_workers):
    run_id = init_drill(runs)
    sup = Supervisor(runs, run_id)
    final = sup.supervise()
    assert final["situation"] == "completed"
    assert sup.decisions == [("validation_failed", "plan", None, "retry"),
                             ("validation_failed", "work", "fan-0001", "retry")]
    assert {c[0] for c in sup.commands} <= {"advance", "retry"}

    st = state_of(runs)
    run_dir = Path(runs) / run_id
    plan_attempts = st["nodes"]["plan"]["attempts"]
    assert [a["kind"] for a in plan_attempts] == ["spawn", "retry"]
    assert len({a["session_id"] for a in plan_attempts}) == 1                      # same session kept
    assert "/steps" in plan_attempts[1]["feedback"] and "received [\"sweep-floor\"]" in plan_attempts[1]["feedback"]
    branches = st["parallel"]["fan"]["branches"]
    failed = branches["fan-0001"]["nodes"]["work"]["attempts"]
    assert [a["kind"] for a in failed] == ["spawn", "retry"] and len({a["session_id"] for a in failed}) == 1
    assert "/done" in failed[1]["feedback"] and 'received "yes"' in failed[1]["feedback"]
    for bid in ("fan-0000", "fan-0002"):                                             # siblings untouched
        assert len(branches[bid]["nodes"]["work"]["attempts"]) == 1

    # bad outputs stay on disk as evidence
    assert json.loads((run_dir / "logs/plan/attempt-1/rejected/plan.json").read_text())["steps"] == ["sweep-floor"]
    assert json.loads((run_dir / "logs/branches/fan-0001/work/attempt-1/rejected/report.json").read_text())["done"] == "yes"

    # the feedback reached the same worker through flowstate, and was recorded as an event
    reg = Registry(run_dir / "workers")
    assert plan_attempts[1]["feedback"] in (reg.invocation_dir("plan", 1) / "input.md").read_text()
    retry_events = [e for e in events_of(runs) if e["type"] == "retry_requested"]
    assert [(e["node"], e.get("branch")) for e in retry_events] == [("plan", None), ("work", "fan-0001")]
    assert all(e["feedback"] for e in retry_events)

    # validation was not bypassed: the corrected outputs were validated and satisfy their schemas
    types = [e["type"] for e in events_of(runs)]
    assert types.index("outputs_validated", types.index("retry_requested")) > types.index("retry_requested")
    jsonschema.validate(json.loads((run_dir / "artefacts/plan.json").read_text()), schema("plan"))
    for path in st["variables"]["report"]:
        jsonschema.validate(json.loads(Path(path).read_text()), schema("report"))
    assert json.loads(Path(st["variables"]["summary"]).read_text())["reports"] == 3

    # every worker came from flowstate via agentctl; none was started any other way
    recorded = {a["worker_id"] for a in plan_attempts} | {
        a["worker_id"] for b in branches.values() for a in b["nodes"]["work"]["attempts"]}
    assert set(reg.worker_ids()) == recorded == {"plan", "work.fan-0000", "work.fan-0001", "work.fan-0002"}
    assert {w: reg.invocation_count(w) for w in reg.worker_ids()} == {
        "plan": 2, "work.fan-0000": 1, "work.fan-0001": 2, "work.fan-0002": 1}


@requires_tmux
def test_supervisor_escalates_retry_then_respawn_then_pauses(prefs, runs, tmp_path, cleanup_workers):
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(always_one_step()))
    run_id = init_drill(runs, plan=plan)
    sup = Supervisor(runs, run_id)
    final = sup.supervise()
    assert final["situation"] == "paused" and "retries_exhausted" not in final["reason"]
    assert sup.decisions == [("validation_failed", "plan", None, "retry"),
                             ("validation_failed", "plan", None, "respawn"),
                             ("validation_failed", "plan", None, "pause")]
    st = state_of(runs)
    attempts = st["nodes"]["plan"]["attempts"]
    assert [a["kind"] for a in attempts] == ["spawn", "retry", "respawn"]
    assert attempts[2]["session_id"] != attempts[0]["session_id"] == attempts[1]["session_id"]
    assert attempts[2]["replaces_session_id"] == attempts[0]["session_id"]
    assert st["situation"]["situation"] == "validation_failed" and st["situation"]["retries_remaining"] == 0
    assert st["pause"]["source"] == "command" and "automatic recovery is not possible" in st["pause"]["reason"]

    # pausing kept the pending decision; resuming returns to it rather than skipping it
    subprocess.run([str(FLOWSTATE), "resume", run_id, "--runs-dir", runs], check=True, capture_output=True)
    again = sup.advance()
    assert (again["situation"], again["max_retries"], again["retries_remaining"]) == ("validation_failed", 2, 0)
    assert len(state_of(runs)["nodes"]["plan"]["attempts"]) == 3


@requires_tmux
def test_supervisor_waits_once_then_respawns_a_stalled_worker(prefs, runs, tmp_path, cleanup_workers):
    prefs(stall_after_s=3)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"invocations": [{"steps": [{"text": "thinking"}, {"sleep": 120}]}]}))
    fixed = (DRILL_FAKES / "plan.json").read_text()

    def replacement_behaves(sit, decision):
        if decision[0] == "respawn":
            spec = json.loads(fixed)
            plan.write_text(json.dumps({"invocations": [spec["invocations"][1]["steps"] and {
                "steps": [{"capture": {"name": "out", "regex": "(\\S+/plan\\.json)"}}] +
                spec["invocations"][1]["steps"][1:]}]}))

    run_id = init_drill(runs, plan=plan)
    sup = Supervisor(runs, run_id, on_decision=replacement_behaves)
    assert sup.supervise()["situation"] == "completed"
    assert sup.decisions[:2] == [("worker_stalled", "plan", None, "wait"), ("worker_stalled", "plan", None, "respawn")]
    assert ["advance", run_id, "--max-wait", "30", "--stall-after", "6.0"] in sup.commands
    attempts = state_of(runs)["nodes"]["plan"]["attempts"]
    assert [a["kind"] for a in attempts] == ["spawn", "respawn"]
    assert attempts[1]["worker_id"] == "plan.respawn-1" and attempts[1]["session_id"] != attempts[0]["session_id"]
    reg = Registry(Path(runs) / run_id / "workers")
    assert lifecycle.status(reg, "plan")["state"] == "killed"                        # stopped by flowstate respawn
    assert "worker_killed" in [e["type"] for e in events_of(runs)]


@requires_tmux
def test_supervisor_retries_an_agent_gate_failure_with_the_gate_evidence(prefs, runs, tmp_path, cleanup_workers):
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"invocations": [
        {"steps": [{"capture": {"name": "out", "regex": "(\\S+/plan\\.json)"}},
                   {"write": {"path": "${out}", "json": {"_session_id": "${session_id}", "goal": "g",
                                                         "steps": ["sweep-floor", "sweep-floor", "label-bins"]}}}]},
        {"steps": [{"capture": {"name": "out", "regex": "(\\S+/plan\\.json)"}},
                   {"write": {"path": "${out}", "json": {"_session_id": "${session_id}", "goal": "g",
                                                         "steps": ["sweep-floor", "wipe-bench", "label-bins"]}}}]}]}))
    run_id = init_drill(runs, plan=plan)
    sup = Supervisor(runs, run_id)
    assert sup.supervise()["situation"] == "completed"
    assert sup.decisions == [("gate_failed", "plan", None, "retry")]
    feedback = state_of(runs)["nodes"]["plan"]["attempts"][1]["feedback"]
    assert "gates/plan-steps-unique.sh" in feedback and "duplicate step names" in feedback
    gate_logs = Path(runs) / run_id / "logs" / "gates" / "plan--fan"
    assert json.loads((gate_logs / "eval-1" / "00-plan-steps-unique.result.json").read_text())["exit_code"] == 1
    assert json.loads((gate_logs / "eval-2" / "00-plan-steps-unique.result.json").read_text())["exit_code"] == 0


@requires_tmux
def test_supervisor_pauses_on_a_deterministic_script_failure(prefs, runs, monkeypatch, cleanup_workers):
    monkeypatch.setenv("DRILL_FINISH_FAIL", "1")
    run_id = init_drill(runs)
    sup = Supervisor(runs, run_id)
    final = sup.supervise()
    assert final["situation"] == "paused"
    assert sup.decisions[-1] == ("script_failed", "finish", None, "pause")
    st = state_of(runs)
    assert len(st["nodes"]["finish"]["attempts"]) == 1                              # not rerun blindly
    assert st["situation"]["situation"] == "script_failed"
    assert "report index is missing" in st["situation"]["stderr_tail"]


def test_supervisor_pauses_when_the_flow_definition_changes(prefs, runs, tmp_path):
    flow = tmp_path / "orchestrator-drill"
    shutil.copytree(DRILL, flow)
    run_id = init_drill(runs, flow=flow)
    (flow / "prompts" / "plan.md").write_text("changed after init\n")
    sup = Supervisor(runs, run_id)
    final = sup.supervise()
    assert final["situation"] == "paused"
    assert sup.decisions == [("flow_changed", None, None, "pause")]
    assert state_of(runs)["nodes"]["plan"]["attempts"] == []                           # nothing ran
