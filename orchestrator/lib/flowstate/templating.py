"""`{var}` substitution and `{include:path}` for prompts, paths and working dirs.

Only `{identifier}` and `{include:relative/path}` are placeholders; any other brace
content (for example JSON examples in prompts) is left untouched. There is no escape
syntax, so literal text is never rewritten. A missing variable is an error, never an
empty string. Included files are inserted verbatim (not re-rendered).
"""

import json
import re
from pathlib import Path

from .errors import FlowstateError

PLACEHOLDER = re.compile(r"\{(?:include:(?P<include>[^{}\s]+)|(?P<name>[A-Za-z_][A-Za-z0-9_]*))\}")


def placeholders(text: str) -> tuple[set[str], list[str]]:
    names, includes = set(), []
    for m in PLACEHOLDER.finditer(text):
        if m.group("include"):
            includes.append(m.group("include"))
        else:
            names.add(m.group("name"))
    return names, includes


def resolve_include(base_dir: Path, rel: str) -> Path:
    base = base_dir.resolve()
    path = (base / rel).resolve()
    if base != path and base not in path.parents:
        raise FlowstateError("include_outside_flow", f"include {rel!r} escapes the flow directory")
    return path


def format_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    return str(value)


def render(text: str, variables: dict, base_dir: Path, what: str) -> str:
    missing = sorted({m.group("name") for m in PLACEHOLDER.finditer(text)
                      if m.group("name") and variables.get(m.group("name")) is None})
    if missing:
        raise FlowstateError("missing_variable", f"{what}: no value for {missing}",
                             {"variables": missing, "template": what})

    def sub(m: re.Match) -> str:
        if m.group("include"):
            path = resolve_include(base_dir, m.group("include"))
            if not path.is_file():
                raise FlowstateError("missing_include", f"{what}: include not found: {path}")
            return path.read_text()
        return format_value(variables[m.group("name")])

    return PLACEHOLDER.sub(sub, text)
