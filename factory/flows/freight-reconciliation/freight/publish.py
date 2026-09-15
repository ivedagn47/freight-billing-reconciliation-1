"""Publish: the last step of a run. Re-verify the report and the memos, then copy both to the destination.

Nothing is published unless:
  - the report satisfies report.schema.json and every report invariant (report.verify), with exactly one row
    per in-scope invoice line;
  - the run's memos directory holds exactly one memo per non-accept line and finding of that report, as
    listed in the render index, and nothing else.

The destination receives reconciliation-report.json and memos/. An existing memos/ is replaced only if it
holds nothing but .md files (a previous publication), so unrelated files are never deleted.
"""

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from . import memos, report
from .coverage import in_scope_line_count


class PublishError(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def publish(report_path: Path, bundle: dict, manifest: dict, memos_index: dict, carriers_cfg: dict,
            schema_path: Path, dest: Path) -> dict:
    report_bytes = Path(report_path).read_bytes()
    rep = json.loads(report_bytes)
    report.verify(rep, bundle["priced"], bundle["planned"], Path(schema_path), in_scope_line_count(manifest))

    problems = []
    expected = {f["memo_id"] for f in memos.memo_facts(bundle, rep, carriers_cfg)}
    listed = {m["memo_id"] for m in memos_index["memos"]}
    source = Path(memos_index["memos_dir"])
    present = {p.stem for p in source.glob("*.md")} if source.is_dir() else set()
    if listed != expected:
        problems.append(f"the memo index lists {len(listed)} memos; the report needs {len(expected)} "
                        f"(missing {sorted(expected - listed)[:5]}, unexpected {sorted(listed - expected)[:5]})")
    if present != expected:
        problems.append(f"{source} holds {len(present)} memos; the report needs {len(expected)}")
    if source.is_dir() and any(p.suffix != ".md" for p in source.iterdir()):
        problems.append(f"{source} holds files that are not memos")

    dest = Path(dest).resolve()
    target = dest / "memos"
    if target.exists() and (not target.is_dir() or any(p.is_dir() or p.suffix != ".md" for p in target.iterdir())):
        problems.append(f"{target} exists and holds something other than memo files; refusing to replace it")
    if problems:
        raise PublishError(problems)

    dest.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=dest, prefix=".memos-publishing-"))
    for memo in sorted(source.glob("*.md")):
        shutil.copy2(memo, staging / memo.name)
    if target.exists():
        shutil.rmtree(target)
    os.replace(staging, target)
    fd, tmp = tempfile.mkstemp(dir=dest, prefix=".report-publishing-")
    with os.fdopen(fd, "wb") as fh:
        fh.write(report_bytes)
    os.replace(tmp, dest / "reconciliation-report.json")
    return {"dest": str(dest), "report": str(dest / "reconciliation-report.json"),
            "report_sha256": hashlib.sha256(report_bytes).hexdigest(), "memos_dir": str(target),
            "memos": sorted(expected), "summary": rep["summary"]}
