"""Phase 6 prompts and isolation flows: prompts render completely, the rate-spec guide matches the code's
vocabularies and its example is a valid spec, and the isolation flows pass flowstate's static checks."""

import json
import re

import pytest

import stage_flows
from flowstate.loader import check_flow
from flowstate.templating import placeholders, render
from freight import ratespec
from freight.vocab import CHARGE_CODES, SHIPMENT_FIELDS

from conftest import FLOW_DIR

GUIDE = (FLOW_DIR / "prompts" / "reference" / "rate-spec-guide.md").read_text()


def test_guide_documents_the_code_vocabularies():
    for code in sorted(set(CHARGE_CODES) - {"credit", "other"}):
        assert f"| `{code}` | {CHARGE_CODES[code]} |" in GUIDE
    for field, kind in SHIPMENT_FIELDS.items():
        if kind in ("number", "string", "list") and field != "delivery_status":
            assert f"| `{field}` |" in GUIDE


def test_guide_example_is_a_valid_rate_spec():
    blocks = [b for b in re.findall(r"```json\n(.*?)```", GUIDE, re.S) if '"spec_version"' in b]
    assert len(blocks) == 1
    ratespec.validate(json.loads(blocks[0]))


PROMPTS = [
    ("extract-rules.md", "rate-spec.json", {"carrier": "acme", "copy": "a"}),
    ("adjudicate.md", "adjudications.json", {"batch_id": "adj-001", "packet": "/p/adj-001.json"}),
    ("write-memos.md", "memo-drafts.json", {"batch_id": "memo-001", "packet": "/p/memo-001.json"}),
]


@pytest.mark.parametrize("name,output,item", PROMPTS)
def test_prompts_render_completely(name, output, item):
    text = (FLOW_DIR / "prompts" / name).read_text()
    assert placeholders(text)[0] == {"item", "_run_artefact_dir"}
    rendered = render(text, {"item": item, "_run_artefact_dir": "/run/artefacts/branches/b"}, FLOW_DIR, name)
    assert f"at exactly this path: /run/artefacts/branches/b/{output}" in rendered
    assert "{include:" not in rendered and json.dumps(item, sort_keys=True) in rendered
    assert "Read no other file" in rendered or "Read only the two files" in rendered


@pytest.mark.parametrize("stage", stage_flows.STAGES)
def test_isolation_flows_pass_static_validation(stage, tmp_path):
    dot = stage_flows.materialize(stage, tmp_path)
    flow, issues = check_flow(dot, dot.with_suffix(".flow.yml"))
    assert issues.errors == [] and flow is not None
