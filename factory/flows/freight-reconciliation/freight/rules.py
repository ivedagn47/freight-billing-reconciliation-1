"""Bookkeeping around the contract-extraction agents: assignments, agreement rounds and the cache.

    plan    per carrier: clause index, cache key, cache lookup; extraction assignments for every
            carrier without a valid cached spec (two independent copies, "a" and "b")
    agree   round 1: both copies must pass tracing; compare them by behaviour (agreement.py).
            Agreed -> the spec is adopted. Disagreed -> two fresh copies ("c", "d") are assigned.
    final   round 2 (if any) must agree the same way, still copy against copy (no majority vote
            with round 1). Adopted specs are written to the cache; anything not agreed stops the run.

Cache key = sha256(contract sha256 | format version | prompt version), where the format version is
the rate-spec version plus a hash of definitions/rate-spec.json, and the prompt version is a hash
of the extraction prompt and the rate-spec guide it includes. A cached spec is re-traced against the
contract and shipment vocabulary on every use, and reused only if it was extracted against the same
shipment vocabulary and invoice charge codes; otherwise it is ignored and the contract is extracted again.

Agreement compares only the charge codes the carrier's invoices can carry (from its formats' parsers):
how a spec maps a code the carrier never bills cannot change a price, so it must not force a rerun.
"""

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import agreement, contracts, documents, ratespec, tracing

FLOW_DIR = Path(__file__).resolve().parents[1]
PROMPT_FILES = ("prompts/extract-rules.md", "prompts/reference/rate-spec-guide.md")
FORMAT_FILE = "definitions/rate-spec.json"
ROUND_COPIES = {1: ("a", "b"), 2: ("c", "d")}


class RulesError(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, data) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


def vocabulary(shipments: list[dict]) -> dict:
    return {"service_level": sorted({s["service_level"] for s in shipments if s.get("service_level")}),
            "special_handling": sorted({h for s in shipments for h in (s.get("special_handling") or [])})}


def format_version(flow_dir: Path = FLOW_DIR) -> str:
    return f"{ratespec.SPEC_VERSION}-{_sha256((Path(flow_dir) / FORMAT_FILE).read_bytes())[:16]}"


def prompt_version(flow_dir: Path = FLOW_DIR) -> str:
    digest = hashlib.sha256()
    for rel in PROMPT_FILES:
        path = Path(flow_dir) / rel
        if not path.is_file():
            raise RulesError([f"extraction prompt file missing: {path}"])
        digest.update(rel.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()[:16]


def cache_key(contract_sha256: str, format_v: str, prompt_v: str) -> str:
    return _sha256(f"{contract_sha256}|{format_v}|{prompt_v}".encode())


def carriers_in_scope(manifest: dict) -> list[str]:
    return sorted(c for c, row in manifest["carriers"].items() if row["in_scope"])


def assignment(carrier: str, copy: str, round_no: int, entry: dict, vocab: dict) -> dict:
    return {"carrier": carrier, "copy": copy, "round": round_no, "contract": entry["contract"],
            "clause_index": entry["clause_index"], "vocabulary": vocab,
            "invoice_charge_codes": entry["invoice_charge_codes"],
            "inputs": [entry["contract"], entry["clause_index"]]}


def _cached_spec_problems(cached: dict, key: str, index: dict, vocab: dict, carrier: str, codes: list[str]) -> list[str]:
    if cached.get("cache_key") != key:
        return [f"cache entry key {cached.get('cache_key')} does not match {key}"]
    if cached.get("extracted_with") != {"vocabulary": vocab, "invoice_charge_codes": codes}:
        return ["the cached spec was extracted against a different shipment vocabulary or invoice charge codes"]
    spec = cached.get("spec")
    if not isinstance(spec, dict):
        return ["cache entry has no spec"]
    problems = tracing.check(spec, index, vocab, carrier)
    if not problems and spec["unrepresentable"]:
        problems.append("cached spec lists unrepresentable terms")
    return problems


def plan(carriers_cfg: dict, carrier_ids: list[str], root: Path, shipments: list[dict], out_dir: Path,
         cache_dir: Path | None, use_cache: bool, flow_dir: Path = FLOW_DIR) -> dict:
    unknown = [c for c in carrier_ids if c not in carriers_cfg]
    if unknown:
        raise RulesError([f"carriers not in the carrier config: {unknown}"])
    if use_cache and cache_dir is None:
        raise RulesError(["a cache directory is required unless the cache is disabled"])
    if use_cache and not Path(cache_dir).is_absolute():
        raise RulesError([f"the cache directory must be an absolute path, got {cache_dir}: script nodes run in the "
                          "run's artefact directory, so a relative path would silently point at a new, empty cache"])
    out_dir = Path(out_dir).resolve()
    vocab = vocabulary(shipments)
    write_json(out_dir / "vocabulary.json", vocab)
    format_v, prompt_v = format_version(flow_dir), prompt_version(flow_dir)
    entries, items = {}, []
    for carrier in sorted(dict.fromkeys(carrier_ids)):
        contract = (Path(root) / carriers_cfg[carrier]["contract"]).resolve()
        index = contracts.index(contract)
        index_path = out_dir / "clauses" / f"{carrier}.json"
        write_json(index_path, index)
        key = cache_key(index["sha256"], format_v, prompt_v)
        entry = {"contract": str(contract), "contract_sha256": index["sha256"], "clause_index": str(index_path),
                 "invoice_charge_codes": documents.invoice_charge_codes(carriers_cfg, carrier),
                 "cache_key": key, "cache": "disabled", "spec": None}
        if use_cache:
            cache_path = Path(cache_dir).resolve() / carrier / f"{key}.json"
            entry["cache_entry"] = str(cache_path)
            if not cache_path.is_file():
                entry["cache"] = "miss"
            else:
                try:
                    cached = json.loads(cache_path.read_text())
                    problems = _cached_spec_problems(cached, key, index, vocab, carrier, entry["invoice_charge_codes"])
                except (OSError, json.JSONDecodeError) as exc:
                    problems = [f"unreadable cache entry: {exc}"]
                if problems:
                    entry.update(cache="invalid", cache_problems=problems)
                else:
                    spec_path = out_dir / "specs" / f"{carrier}.json"
                    write_json(spec_path, cached["spec"])
                    entry.update(cache="hit", spec=str(spec_path), cached_agreement=cached.get("agreement"))
        if entry["cache"] != "hit":
            items += [assignment(carrier, copy, 1, entry, vocab) for copy in ROUND_COPIES[1]]
        entries[carrier] = entry
    return {"format_version": format_v, "prompt_version": prompt_v,
            "cache_dir": str(Path(cache_dir).resolve()) if cache_dir else None, "use_cache": use_cache,
            "clauses_dir": str(out_dir / "clauses"),
            "vocabulary": vocab, "carriers": entries, "extraction_items": items}


def _round(plan_doc: dict, items: list[dict], spec_paths: list[str], round_no: int, out_dir: Path) -> dict:
    if len(items) != len(spec_paths):
        raise RulesError([f"round {round_no}: {len(items)} extraction assignments but {len(spec_paths)} rate specs"])
    groups: dict[str, list] = {}
    for item, path in zip(items, spec_paths):
        if item["round"] != round_no:
            raise RulesError([f"assignment {item['carrier']}/{item['copy']} belongs to round {item['round']}"])
        groups.setdefault(item["carrier"], []).append((item, Path(path)))

    results = {}
    for carrier, copies in sorted(groups.items()):
        index = json.loads(Path(plan_doc["carriers"][carrier]["clause_index"]).read_text())
        loaded, problems = [], []
        for item, path in copies:
            spec = json.loads(path.read_text())
            problems += [f"copy {item['copy']}: {p}" for p in tracing.check(spec, index, plan_doc["vocabulary"], carrier)]
            loaded.append({"copy": item["copy"], "path": str(path), "session_id": spec.get("_session_id"),
                           "sha256": _sha256(path.read_bytes()), "spec": spec})
        sessions = [c["session_id"] for c in loaded]
        if len(loaded) != 2:
            problems.append(f"expected two independent copies, got {len(loaded)}")
        elif None in sessions or len(set(sessions)) != len(sessions):
            problems.append("the copies must come from distinct worker sessions")
        record = {"round": round_no, "copies": [{k: c[k] for k in ("copy", "path", "session_id", "sha256")}
                                                for c in loaded]}
        unrepresentable = [{"copy": c["copy"], **u} for c in loaded for u in c["spec"].get("unrepresentable") or []]
        if problems:
            record.update(status="invalid", problems=problems)
        elif unrepresentable:
            record.update(status="unrepresentable", unrepresentable=unrepresentable)
        else:
            try:
                comparison = agreement.compare(loaded[0]["spec"], loaded[1]["spec"], plan_doc["vocabulary"],
                                               plan_doc["carriers"][carrier]["invoice_charge_codes"])
            except agreement.AgreementError as exc:
                record.update(status="incomparable", problems=[str(exc)])
            else:
                record["comparison"] = comparison
                if comparison["agree"]:
                    spec_path = Path(out_dir).resolve() / "specs" / f"{carrier}.json"
                    write_json(spec_path, loaded[0]["spec"])
                    record.update(status="agreed", adopted_copy=loaded[0]["copy"], spec=str(spec_path))
                else:
                    record["status"] = "disagreed"
        results[carrier] = record
    return results


def agree(plan_doc: dict, spec_paths: list[str], out_dir: Path) -> tuple[dict, dict]:
    """Round 1. Returns (record, blocking statuses by carrier)."""
    results = _round(plan_doc, plan_doc["extraction_items"], spec_paths, 1, out_dir)
    rerun = [assignment(c, copy, 2, plan_doc["carriers"][c], plan_doc["vocabulary"])
             for c, r in results.items() if r["status"] == "disagreed" for copy in ROUND_COPIES[2]]
    blocking = {c: r["status"] for c, r in results.items() if r["status"] not in ("agreed", "disagreed")}
    return {"round": 1, "carriers": results, "rerun_items": rerun}, blocking


def _write_cache(plan_doc: dict, carrier: str, record: dict, run_id: str | None) -> str:
    entry = plan_doc["carriers"][carrier]
    spec = json.loads(Path(record["spec"]).read_text())
    path = Path(entry["cache_entry"])
    write_json(path, {
        "cache_key": entry["cache_key"],
        "key_parts": {"contract_sha256": entry["contract_sha256"], "format_version": plan_doc["format_version"],
                      "prompt_version": plan_doc["prompt_version"]},
        "carrier": carrier, "contract_file": spec["contract_file"], "spec": spec,
        "extracted_with": {"vocabulary": plan_doc["vocabulary"], "invoice_charge_codes": entry["invoice_charge_codes"]},
        "agreement": {"round": record["round"], "copies": [{k: c[k] for k in ("copy", "session_id", "sha256")}
                                                           for c in record["copies"]],
                      "probes": record["comparison"]["probes"], "run_id": run_id},
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    return str(path)


def final(plan_doc: dict, round1: dict, spec_paths: list[str], out_dir: Path, run_id: str | None = None
          ) -> tuple[dict, dict]:
    """Round 2 (if any), then the adopted spec per carrier and cache writes."""
    round2 = _round(plan_doc, round1["rerun_items"], spec_paths, 2, out_dir) if round1["rerun_items"] or spec_paths \
        else {}
    specs, sources, blocking, cache_writes = {}, {}, {}, []
    for carrier, entry in sorted(plan_doc["carriers"].items()):
        record = round2.get(carrier) or round1["carriers"].get(carrier)
        if entry["cache"] == "hit":
            specs[carrier] = entry["spec"]
            sources[carrier] = {"source": "cache", "cache_entry": entry["cache_entry"]}
            continue
        if record is None or record["status"] != "agreed":
            blocking[carrier] = "missing" if record is None else record["status"]
            continue
        specs[carrier] = record["spec"]
        sources[carrier] = {"source": f"round {record['round']}", "adopted_copy": record["adopted_copy"],
                            "sessions": [c["session_id"] for c in record["copies"]]}
        if record["round"] == 2:
            adopted = json.loads(Path(record["spec"]).read_text())
            matches = []
            for copy in round1["carriers"][carrier]["copies"]:
                earlier = json.loads(Path(copy["path"]).read_text())
                if agreement.compare(adopted, earlier, plan_doc["vocabulary"], entry["invoice_charge_codes"])["agree"]:
                    matches.append(copy["copy"])
            sources[carrier]["matches_round1_copies"] = matches
        if plan_doc["use_cache"]:
            cache_writes.append(_write_cache(plan_doc, carrier, record, run_id))
    return {"specs": specs, "sources": sources, "round2": round2, "cache_writes": cache_writes,
            "blocking": blocking}, blocking
