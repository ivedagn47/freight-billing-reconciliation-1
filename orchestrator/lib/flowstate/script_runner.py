"""Runs one script execution detached from `flowstate advance`, so it survives the CLI exiting
and several can run in parallel.

    python script_runner.py <log_dir> <stem>

Reads <stem>.command.json (argv, cwd, timeout_s) and writes <stem>.stdout.log,
<stem>.stderr.log, <stem>.result.json and, last, <stem>.exit_code, the completion marker.
The environment is inherited from the launching process; command.json only records the
flowstate-provided variables as evidence, so inherited secrets are never written to disk.
Standard library only, run by file path, so it needs no PYTHONPATH.
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def _write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)


def main(log_dir: str, stem: str) -> int:
    base = Path(log_dir)
    command = json.loads((base / f"{stem}.command.json").read_text())
    out_path, err_path = base / f"{stem}.stdout.log", base / f"{stem}.stderr.log"
    started = time.monotonic()
    exit_code, timed_out = None, False
    with open(out_path, "wb") as out, open(err_path, "wb") as err:
        try:
            proc = subprocess.Popen(command["argv"], cwd=command["cwd"], stdout=out, stderr=err,
                                    stdin=subprocess.DEVNULL)
        except OSError as exc:
            err.write(f"flowstate: failed to start {command['argv'][0]}: {exc}\n".encode())
            exit_code = 127
        else:
            def forward(signum, _frame):
                if proc.poll() is None:
                    proc.send_signal(signum)

            for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
                signal.signal(sig, forward)
            try:
                exit_code = proc.wait(timeout=command.get("timeout_s"))
            except subprocess.TimeoutExpired:
                timed_out = True
                proc.kill()
                proc.wait()
                exit_code = proc.returncode
    result = {"argv": command["argv"], "cwd": command["cwd"], "exit_code": exit_code, "timed_out": timed_out,
              "duration_s": round(time.monotonic() - started, 3),
              "stdout": str(out_path), "stderr": str(err_path)}
    _write_atomic(base / f"{stem}.result.json", json.dumps(result, indent=2) + "\n")
    _write_atomic(base / f"{stem}.exit_code", f"{exit_code}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
