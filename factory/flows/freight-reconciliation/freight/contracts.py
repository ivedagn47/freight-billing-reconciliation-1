"""Contract clause index.

Turns a contract's markdown into addressable clauses so that rate-spec values can be traced to
the clause they come from, and justifications and memos can quote clause text. It does not
interpret prices: it only records structure, text and the numbers each clause contains.

    {file, sha256, title, agreement_ref, parties, service, term: {start, end, text},
     clauses: [{id, section, text, numbers: ["9.5", "25", ...]}]}
"""

import hashlib
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

MONTHS = {m: i for i, m in enumerate(("january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"), 1)}
META = re.compile(r"^\*\*(?P<key>[^*]+):\*\*\s*(?P<value>.+)$")
SECTION = re.compile(r"^##\s+(?P<title>.+)$")
CLAUSE = re.compile(r"^(?P<id>\d+)\.\s+(?P<text>.*)$")
DAY_MONTH_YEAR = re.compile(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})")
NUMBER = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?(?![\w])")


class ContractError(Exception):
    pass


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("**", "")).strip()


def numbers(text: str) -> list[str]:
    """Numeric tokens in canonical form ("2,000" -> "2000", "9.50" -> "9.5")."""
    out = []
    for whole, frac in NUMBER.findall(text):
        value = Decimal(whole.replace(",", "") + (f".{frac}" if frac else ""))
        normalized = format(value.normalize(), "f")
        if normalized not in out:
            out.append(normalized)
    return out


def _dates(text: str) -> list[str]:
    found = []
    for day, month, year in DAY_MONTH_YEAR.findall(text):
        if month.lower() not in MONTHS:
            raise ContractError(f"unrecognised month {month!r} in {text!r}")
        found.append(date(int(year), MONTHS[month.lower()], int(day)).isoformat())
    return found


def index(path: Path) -> dict:
    path = Path(path)
    lines = path.read_text().splitlines()
    doc = {"file": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "title": None,
           "agreement_ref": None, "parties": None, "service": None, "term": None, "clauses": []}
    section, current = None, None

    def close():
        nonlocal current
        if current is not None:
            current["text"] = _clean(" ".join(current.pop("parts")))
            current["numbers"] = numbers(current["text"])
            doc["clauses"].append(current)
            current = None

    for raw in lines:
        stripped = raw.strip()
        if raw.startswith("# ") and doc["title"] is None:
            doc["title"] = _clean(raw[2:])
            continue
        if m := META.match(stripped):
            key, value = m["key"].strip().lower(), _clean(m["value"])
            if key == "agreement ref":
                doc["agreement_ref"] = value
            elif key == "between":
                doc["parties"] = value
            elif key == "service":
                doc["service"] = value
            elif key == "term":
                found = _dates(value)
                if len(found) != 2:
                    raise ContractError(f"{path.name}: term {value!r} does not contain a start and end date")
                doc["term"] = {"start": found[0], "end": found[1], "text": value}
            continue
        if m := SECTION.match(stripped):
            close()
            section = _clean(m["title"])
            continue
        if (m := CLAUSE.match(raw)) and not raw.startswith(" "):
            close()
            current = {"id": m["id"], "section": section, "parts": [m["text"]]}
            continue
        if current is not None and stripped:
            current["parts"].append(stripped)

    close()
    ids = [c["id"] for c in doc["clauses"]]
    if len(ids) != len(set(ids)):
        raise ContractError(f"{path.name}: duplicate clause numbers {ids}")
    if not doc["clauses"]:
        raise ContractError(f"{path.name}: no numbered clauses found")
    return doc


def clause(index_doc: dict, clause_id: str) -> dict | None:
    return next((c for c in index_doc["clauses"] if c["id"] == str(clause_id)), None)
