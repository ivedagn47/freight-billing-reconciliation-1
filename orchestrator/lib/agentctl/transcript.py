"""Parse Claude Code stream-json transcripts (one JSON event per line).

The fake harness emits the same event shapes, so everything downstream is
harness-agnostic."""

import json
from pathlib import Path
from typing import Iterator

INIT_EVIDENCE_KEYS = ("tools", "skills", "slash_commands", "plugins", "mcp_servers",
                      "agents", "memory_paths", "messaging_socket_path", "permissionMode")


def iter_events(path: Path) -> Iterator[dict]:
    try:
        data = Path(path).read_bytes()
    except FileNotFoundError:
        return
    for raw in data.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue  # a line still being written, or non-JSON noise
        if isinstance(event, dict):
            yield event


def summarize(path: Path) -> dict:
    init = result = None
    last_text = None
    count = 0
    for event in iter_events(path):
        count += 1
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            init = event
        elif kind == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    last_text = block.get("text")
        elif kind == "result":
            result = event

    summary = {
        "events": count,
        "session_id": (result or init or {}).get("session_id"),
        "model": (init or {}).get("model"),
        "completed": result is not None,
        "is_error": None,
        "subtype": None,
        "cost_usd": None,
        "num_turns": None,
        "permission_denials": None,
        "result_text": last_text,
        "init": {k: init.get(k) for k in INIT_EVIDENCE_KEYS} if init else None,
    }
    if result is not None:
        summary.update(
            is_error=bool(result.get("is_error")),
            subtype=result.get("subtype"),
            cost_usd=result.get("total_cost_usd"),
            num_turns=result.get("num_turns"),
            permission_denials=result.get("permission_denials") or [],
            result_text=result.get("result", last_text),
        )
    return summary
