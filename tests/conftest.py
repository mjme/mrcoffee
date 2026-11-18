import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
FAKE_CAFFEINATE = str(FIXTURES / "fake_caffeinate.py")
HEADLESS = str(FIXTURES / "headless.py")


def pytest_collection_modifyitems(items):
    if sys.platform != "darwin":
        skip = pytest.mark.skip(reason="needs macOS")
        for item in items:
            if "macos" in item.keywords:
                item.add_marker(skip)


def alive(pid: int) -> bool:
    try:
        os.waitpid(pid, os.WNOHANG)  # reap it first if it is our own dead child
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_for(predicate, timeout=3.0):
    """Poll until predicate() is truthy and return its value; fail the test on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if value := predicate():
            return value
        time.sleep(0.02)
    pytest.fail(f"timed out after {timeout}s waiting for {predicate.__name__}")


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """Every test gets its own MRCOFFEE_HOME and HOME, so nothing real is ever touched."""
    (tmp_path / "home").mkdir()
    (tmp_path / "mrcoffee").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MRCOFFEE_HOME", str(tmp_path / "mrcoffee"))
    monkeypatch.setattr("mrcoffee.install._launchctl", _no_real_launchctl)
    return tmp_path / "mrcoffee"


def _no_real_launchctl(*args):
    raise AssertionError(f"a test tried to run the real launchctl: {args}")


class Procs:
    """Records every process a test starts so the teardown can check none is left behind."""

    def __init__(self, home: Path):
        self.home = home
        self.pids: list[int] = []

    def spawn(self, *args, **kwargs) -> subprocess.Popen:
        proc = subprocess.Popen(args, **kwargs)
        self.pids.append(proc.pid)
        return proc

    def track(self, pid: int) -> int:
        self.pids.append(pid)
        return pid

    def caffeinates(self) -> list[dict]:
        """One {"pid", "argv"} record per fake caffeinate started so far."""
        path = self.home / "spawned"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture(autouse=True)
def procs(home):
    tracker = Procs(home)
    yield tracker
    stray = [pid for pid in tracker.pids + [c["pid"] for c in tracker.caffeinates()] if alive(pid)]
    for pid in stray:
        os.kill(pid, signal.SIGKILL)
    if stray:
        pytest.fail(f"stray processes left behind: {stray}")
