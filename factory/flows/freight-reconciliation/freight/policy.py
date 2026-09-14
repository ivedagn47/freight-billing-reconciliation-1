"""Disposition policy: turn pricing flags into dispositions, or into a bounded choice.

A line is decided here when exactly one disposition is allowed. When a `judgement` flag leaves two
options, the line goes to adjudication with those options and nothing else; the adjudicator can
choose between them but cannot change any amount. Unknown flags fail closed.
"""

from pathlib import Path

import yaml

from .money import to_decimal
from .pricing import FINDING_CODES, LINE_FLAG_CODES

EFFECTS = ("escalate", "offset", "dispute", "judgement", "info")
DISPOSITIONS = ("accept", "dispute", "escalate")
DIRECTIONS = ("over", "under", "undetermined")


class PolicyError(Exception):
    pass


def load(path: Path) -> dict:
    data = yaml.safe_load(Path(path).read_text()) or {}
    try:
        to_decimal(str(data["tolerance_inr"]))
    except (KeyError, ValueError) as exc:
        raise PolicyError("tolerance_inr must be a decimal amount") from exc
    flags = data.get("line_flags") or {}
    bad = {k: v for k, v in flags.items() if v not in EFFECTS}
    if bad:
        raise PolicyError(f"unknown effects {bad}; use one of {EFFECTS}")
    if set(flags) != LINE_FLAG_CODES:
        raise PolicyError(f"line_flags must cover exactly the pricing flags; missing "
                          f"{sorted(LINE_FLAG_CODES - set(flags))}, unknown {sorted(set(flags) - LINE_FLAG_CODES)}")
    findings = data.get("invoice_findings") or {}
    if set(findings) != FINDING_CODES:
        raise PolicyError(f"invoice_findings must cover exactly {sorted(FINDING_CODES)}")
    for code, rule in findings.items():
        if not isinstance(rule, dict) or not set(rule) <= set(DIRECTIONS) or not set(rule.values()) <= set(DISPOSITIONS):
            raise PolicyError(f"invoice_findings.{code} must map directions to dispositions")
    return data


def decide_line(line: dict, policy: dict) -> dict:
    tolerance = to_decimal(str(policy["tolerance_inr"]))
    effects = {}
    for f in line["flags"]:
        if f["code"] not in policy["line_flags"]:
            raise PolicyError(f"{line['item_id']}: flag {f['code']} has no policy")
        effects.setdefault(policy["line_flags"][f["code"]], []).append(f["code"])

    if line["expected_amount"] is None or "escalate" in effects:
        return {"disposition": "escalate", "options": None,
                "basis": effects.get("escalate") or ["expected amount undetermined"]}
    delta = to_decimal(line["delta"])
    outcome, basis = ("dispute", ["billed exceeds expected"]) if delta > tolerance else ("accept", ["within tolerance"]
                                                                                           if delta >= -tolerance
                                                                                           else ["underbilled"])
    if "offset" in effects:
        outcome, basis = "accept", effects["offset"]
    elif "dispute" in effects:
        outcome, basis = "dispute", effects["dispute"]
    if "judgement" in effects:
        return {"disposition": None, "options": sorted({outcome, "escalate"}), "basis": basis + effects["judgement"]}
    return {"disposition": outcome, "options": None, "basis": basis}


def decide_finding(finding: dict, policy: dict) -> str:
    rule = policy["invoice_findings"].get(finding["code"])
    if rule is None:
        raise PolicyError(f"{finding['finding_id']}: finding {finding['code']} has no policy")
    if finding["direction"] not in rule:
        raise PolicyError(f"{finding['finding_id']}: no policy for direction {finding['direction']!r}")
    return rule[finding["direction"]]


def apply(priced: dict, policy: dict) -> dict:
    """Attach decisions. Returns {decisions, needs_judgement} keyed by item/finding id."""
    decisions, needs_judgement = {}, []
    for line in priced["lines"]:
        decision = decide_line(line, policy)
        decisions[line["item_id"]] = decision
        if decision["disposition"] is None:
            needs_judgement.append({"item_id": line["item_id"], "options": decision["options"],
                                    "basis": decision["basis"]})
    for finding in priced["invoice_findings"]:
        decisions[finding["finding_id"]] = {"disposition": decide_finding(finding, policy), "options": None,
                                            "basis": [finding["code"]]}
    return {"decisions": decisions, "needs_judgement": needs_judgement}
