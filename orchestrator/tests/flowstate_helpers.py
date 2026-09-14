"""Build small throwaway flows for tests (the real demo flows are used as-is elsewhere)."""

import json
import textwrap
from pathlib import Path

NOTE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object", "additionalProperties": False,
    "required": ["_session_id", "note"],
    "properties": {"_session_id": {"type": "string"}, "note": {"type": "string"},
                   "count": {"type": "integer"}},
}


def write_flow(root: Path, name: str, dot: str, yml: str, files: dict[str, str] | None = None,
               schemas: dict[str, dict] | None = None) -> Path:
    flow_dir = root / name
    (flow_dir / "definitions").mkdir(parents=True, exist_ok=True)
    (flow_dir / f"{name}.dot").write_text(textwrap.dedent(dot))
    (flow_dir / f"{name}.flow.yml").write_text(textwrap.dedent(yml))
    for rel, content in (files or {}).items():
        path = flow_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content))
    for sname, schema in (schemas or {"note": NOTE_SCHEMA}).items():
        (flow_dir / "definitions" / f"{sname}.json").write_text(json.dumps(schema))
    return flow_dir / f"{name}.dot"


SCRIPT_FLOW_DOT = """
digraph s {
  start [shape=Mdiamond]
  make  [shape=box, runner=script, script="scripts/make.sh", working_dir="{_run_artefact_dir}",
         output_schema="make_outputs"]
  done  [shape=Msquare]
  start -> make
  make -> done [gates="gates/check.sh"]
}
"""
SCRIPT_FLOW_YML = """
output_schemas:
  make_outputs:
    files:
      - name: note
        path: "{_run_artefact_dir}/note.json"
        definition: note
    sets_variables:
      note_path: note
      count: {file: note, pointer: /count}
variables:
  greeting: {type: string}
  note_path: {type: path}
  count: {type: integer}
"""
SCRIPT_FLOW_FILES = {
    "scripts/make.sh": """\
        #!/usr/bin/env bash
        set -euo pipefail
        [ -n "${MAKE_FAIL:-}" ] && { echo "asked to fail" >&2; exit 3; }
        printf '{"_session_id": "script", "note": "%s", "count": %s}' "$FLOWSTATE_VAR_greeting" "${MAKE_COUNT:-2}" > "$FLOWSTATE_VAR_note_path"
        """,
    "gates/check.sh": """\
        #!/usr/bin/env bash
        [ -s "$FLOWSTATE_VAR_note_path" ] || { echo "note missing" >&2; exit 1; }
        [ -z "${GATE_FAIL:-}" ] || { echo "gate told to fail" >&2; exit 4; }
        """,
}
