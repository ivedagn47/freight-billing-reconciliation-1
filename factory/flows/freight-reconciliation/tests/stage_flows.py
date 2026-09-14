"""Isolation flows for the Phase 6 agent nodes, and synthetic inputs for them.

A stage flow (tests/stages/<stage>.dot + .flow.yml) runs one agent node with its real prompt, output
schema, gates and scripts. `materialize` copies the flow directory's assets next to the stage files, so
flowstate resolves every reference exactly as it will in the reconciliation flow, without adding test
graphs to the product directory.

The synthetic inputs use the invented `acme` carrier only; nothing here encodes a real contract.
"""

import json
import shutil
import sys
from pathlib import Path

FLOW_DIR = Path(__file__).resolve().parents[1]
REPO = FLOW_DIR.parents[2]
STAGES_DIR = Path(__file__).resolve().parent / "stages"
ASSETS = ("freight", "config", "definitions", "prompts", "gates", "scripts")
ORCHESTRATOR_LIB = REPO / "orchestrator" / "lib"

for path in (str(FLOW_DIR), str(ORCHESTRATOR_LIB)):
    if path not in sys.path:
        sys.path.insert(0, path)

from freight import contracts, policy, pricing, ratespec, report  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from synthetic import ACME_CONTRACT, credit_note, invoice, line, shipment, spec  # noqa: E402

STAGES = ("stage-rules", "stage-adjudicate", "stage-memos")


def materialize(stage: str, dest_root: Path) -> Path:
    dest = Path(dest_root) / stage
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for name in ASSETS:
        shutil.copytree(FLOW_DIR / name, dest / name, ignore=shutil.ignore_patterns("__pycache__"))
    for suffix in (".dot", ".flow.yml"):
        shutil.copy2(STAGES_DIR / f"{stage}{suffix}", dest / f"{stage}{suffix}")
    return dest / f"{stage}.dot"


def write_json(path: Path, data) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return path


def acme_inputs(root: Path) -> dict:
    """Synthetic carrier config, contract, shipments and clause index for the acme carrier."""
    root = Path(root)
    contract = root / "data" / "contracts" / "acme.md"
    contract.parent.mkdir(parents=True, exist_ok=True)
    contract.write_text(ACME_CONTRACT)
    carriers = root / "carriers.yml"
    carriers.write_text("carriers:\n  acme:\n    name: Acme Test Carriers\n    contract: data/contracts/acme.md\n"
                        "    consignment_prefix: AC-\n    invoice_formats: [alpine-json]\n")
    ships = [shipment("AC-1", 50), shipment("AC-3", 5, special_handling=["residential"]),
             shipment("AC-5", 200, delivery_status="in_transit"),
             shipment("AC-7", 60, service_level="express", special_handling=["fragile"])]
    shipments_file = write_json(root / "shipments.json", ships)
    clauses_dir = root / "clauses"
    write_json(clauses_dir / "acme.json", contracts.index(contract))
    return {"root": root, "carriers": carriers, "contract": contract, "shipments": shipments_file,
            "shipment_records": ships, "clauses_dir": clauses_dir}


def acme_priced(root: Path) -> dict:
    """A priced bundle with lines policy leaves open (not delivered; service not offered), and the report
    assembled from fixed adjudications, for exercising the adjudication and memo stages."""
    inputs = acme_inputs(root)
    inv = invoice("ACME-07", [
        line(1, "AC-1", [("freight_incl_fuel", "121.00")]),
        line(2, "AC-3", [("freight_incl_fuel", "22.00"), ("residential_delivery", "50.00")]),
        line(3, "AC-5", [("freight_incl_fuel", "330.00"), ("detention", "40.00")]),
        line(4, "AC-7", [("freight_incl_fuel", "132.00")]),
        line(5, "AC-9", [("freight", "10.00")]),
    ])
    cn = credit_note("CN-1", "ACME-07", [line(1, "AC-1", [("credit", "-11.00")])])
    rules_doc = policy.load(FLOW_DIR / "config" / "policy.yml")
    priced = pricing.price([inv, cn], ["ACME-07", "CN-1"], inputs["shipment_records"],
                           {"acme": ratespec.validate(spec())}, str(rules_doc["tolerance_inr"]))
    planned = policy.apply(priced, rules_doc)
    bundle = {"priced": priced, "planned": planned}
    decisions = {n["item_id"]: {"disposition": "escalate", "justification": "A person must decide this line.",
                                "clauses": []} for n in planned["needs_judgement"]}
    assembled = report.assemble(priced, planned, rules_doc, decisions)
    report.verify(assembled, priced, planned, REPO / "report.schema.json")
    return {**inputs, "bundle": bundle, "priced": write_json(Path(root) / "priced.json", bundle),
            "report_doc": assembled, "report": write_json(Path(root) / "report.json", assembled)}
