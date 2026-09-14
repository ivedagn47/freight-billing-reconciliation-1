"""Format registry. A file must be claimed by exactly one parser; otherwise discovery stops."""

from pathlib import Path

from . import alpine_json, falcon_text, sagar_csv
from .base import ParseError

FORMATS = {m.NAME: m for m in (alpine_json, falcon_text, sagar_csv)}


def detect(path: Path) -> str:
    head = path.read_text(errors="replace")[:4096]
    matches = [name for name, module in FORMATS.items() if module.sniff(path, head)]
    if len(matches) != 1:
        raise ParseError(path, "no parser recognises this file" if not matches
                         else f"several parsers claim this file: {matches}")
    return matches[0]


def parse_file(path: Path) -> list[dict]:
    fmt = detect(path)
    return FORMATS[fmt].parse(path, path.read_text())
