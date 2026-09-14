import json
import shutil
import sys
import uuid
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))

from agentctl import tmux  # noqa: E402
from agentctl.registry import Registry  # noqa: E402

requires_tmux = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")


@pytest.fixture
def registry(tmp_path):
    reg = Registry(tmp_path / "workers")
    yield reg
    for name in tmux.list_sessions(reg.tmux_prefix):
        tmux.kill_session(name)


@pytest.fixture
def workdir(tmp_path):
    path = tmp_path / "work"
    path.mkdir()
    return path


@pytest.fixture
def fake_script(tmp_path):
    def make(*invocations, model="fake"):
        path = tmp_path / f"script-{uuid.uuid4().hex[:8]}.json"
        path.write_text(json.dumps({"model": model, "invocations": list(invocations)}))
        return str(path)
    return make
