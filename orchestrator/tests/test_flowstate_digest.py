"""Phase 7: flow.yml `digest_include` extends the flow digest to files a flow depends on indirectly (an
imported package, config), so changing them after init is reported as flow_changed."""

import pytest
import yaml

from flowstate import engine
from flowstate.loader import check_flow, load_flow

from flowstate_helpers import SCRIPT_FLOW_DOT, SCRIPT_FLOW_FILES, SCRIPT_FLOW_YML, write_flow

INCLUDED = SCRIPT_FLOW_YML + 'digest_include: ["lib/**/*.py", "config.yml"]\n'
FILES = {**SCRIPT_FLOW_FILES, "lib/helper.py": "VALUE = 1\n", "lib/sub/more.py": "OTHER = 2\n",
         "config.yml": "rate: 3\n", "lib/notes.txt": "not matched\n"}


def test_included_files_change_the_digest(tmp_path):
    dot = write_flow(tmp_path, "s", SCRIPT_FLOW_DOT, INCLUDED, FILES)
    yml = dot.parent / "s.flow.yml"
    before = load_flow(dot, yml).digest
    (dot.parent / "lib" / "notes.txt").write_text("not part of the digest\n")
    assert load_flow(dot, yml).digest == before
    (dot.parent / "lib" / "sub" / "more.py").write_text("OTHER = 3\n")
    assert load_flow(dot, yml).digest != before


@pytest.mark.parametrize("patterns,message", [
    (["../outside/*.py"], "must stay inside the flow directory"),
    (["/etc/*.conf"], "must stay inside the flow directory"),
    (["missing/*.py"], "matches no files"),
    ("lib/*.py", "must be a list of glob patterns"),
])
def test_invalid_patterns_are_reported(tmp_path, patterns, message):
    yml = SCRIPT_FLOW_YML + yaml.safe_dump({"digest_include": patterns})
    dot = write_flow(tmp_path, "s", SCRIPT_FLOW_DOT, yml, FILES)
    _, issues = check_flow(dot, dot.parent / "s.flow.yml")
    found = [i for i in issues.errors if i["code"] == "invalid_digest_include"]
    assert found and message in found[0]["message"]


def test_changing_an_included_file_after_init_is_flow_changed(tmp_path, monkeypatch):
    prefs = tmp_path / "prefs.yml"
    prefs.write_text("supervision: low\n")
    monkeypatch.setenv("FLOWSTATE_PREFS", str(prefs))
    dot = write_flow(tmp_path, "s", SCRIPT_FLOW_DOT, INCLUDED, FILES)
    runs = str(tmp_path / "runs")
    engine.init_run(str(dot), ["greeting=hi"], run_id="r1", runs_dir=runs)
    (dot.parent / "config.yml").write_text("rate: 4\n")
    assert engine.advance("r1", runs_dir=runs)["situation"] == "flow_changed"
