"""Layer 2: the real SubprocessRunner against a stand-in caffeinate (runs on Linux and macOS)."""

import os
import signal
import subprocess
import sys
import time

import pytest

from conftest import FAKE_CAFFEINATE, HEADLESS, alive, wait_for
from mrcoffee import db
from mrcoffee.reconciler import ERROR, Reconciler
from mrcoffee.runner import SubprocessRunner, single_instance_lock

FLAGS = ("-i", "-s")
DISPLAY_FLAGS = ("-d", "-i", "-s")


@pytest.fixture
def runner():
    runner = SubprocessRunner(executable=FAKE_CAFFEINATE, term_timeout=2.0)
    yield runner
    runner.shutdown()


def started(procs, count):
    """Wait until `count` fake caffeinates have reported in, and return their records."""
    return wait_for(lambda: len(procs.caffeinates()) >= count and procs.caffeinates())


def test_i1_child_is_started_with_w_and_our_own_pid(runner, procs):
    runner.ensure(FLAGS)
    record = started(procs, 1)[0]
    assert (record["pid"], record["argv"]) == (runner.pid, ["-i", "-s", "-w", str(os.getpid())])


def test_i2_changing_flags_stops_the_old_child_before_starting_the_new_one(runner, procs):
    runner.ensure(FLAGS)
    first = started(procs, 1)[0]["pid"]  # wait for it to report in before replacing it
    runner.ensure(DISPLAY_FLAGS)

    assert not alive(first)
    assert runner.pid != first and alive(runner.pid)
    records = started(procs, 2)
    assert records[1]["argv"] == ["-d", "-i", "-s", "-w", str(os.getpid())]
    assert [alive(r["pid"]) for r in records] == [False, True]
    assert records[1]["alive_at_start"] == []  # the old child was already gone when the new one started


def test_i2_new_child_starts_only_after_a_stubborn_old_one_is_confirmed_gone(procs, monkeypatch):
    monkeypatch.setenv("FAKE_CAFFEINATE_IGNORE_TERM", "1")  # the old child survives SIGTERM
    runner = SubprocessRunner(executable=FAKE_CAFFEINATE, term_timeout=0.3)
    try:
        runner.ensure(FLAGS)
        first = started(procs, 1)[0]["pid"]
        runner.ensure(DISPLAY_FLAGS)
        second = started(procs, 2)[1]
        assert second["alive_at_start"] == [] and not alive(first)
    finally:
        runner.shutdown()


def test_i3_stopping_reaps_the_child_so_no_zombie_remains(runner):
    runner.ensure(FLAGS)
    pid = runner.pid
    runner.ensure(None)

    assert runner.pid is None
    with pytest.raises(ChildProcessError):  # already wait()ed for: nothing left to reap
        os.waitpid(pid, os.WNOHANG)
    ps = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True)
    assert ps.stdout.strip() == ""


def test_i3_sigkill_follows_when_sigterm_is_ignored(procs, monkeypatch):
    monkeypatch.setenv("FAKE_CAFFEINATE_IGNORE_TERM", "1")
    runner = SubprocessRunner(executable=FAKE_CAFFEINATE, term_timeout=0.3)
    runner.ensure(FLAGS)
    pid = started(procs, 1)[0]["pid"]  # reported in, so SIGTERM is already being ignored

    began = time.monotonic()
    runner.shutdown()

    assert time.monotonic() - began >= 0.3
    assert not alive(pid)


def test_i4_shutdown_is_idempotent_and_safe_when_nothing_runs(runner):
    runner.shutdown()
    runner.ensure(FLAGS)
    pid = runner.pid
    runner.shutdown()
    runner.shutdown()
    assert runner.pid is None and not alive(pid)


def test_i5_sweep_terminates_a_stale_caffeinate(procs):
    stale = procs.spawn(FAKE_CAFFEINATE, "-i", "-s", "-w", str(os.getpid()))
    started(procs, 1)

    assert SubprocessRunner(executable=FAKE_CAFFEINATE, term_timeout=0.3).sweep(stale.pid) is True
    assert stale.wait(timeout=2) == -signal.SIGTERM


def test_i5_sweep_never_kills_an_unrelated_process(procs):
    bystander = procs.spawn("sleep", "30")
    try:
        assert SubprocessRunner(executable=FAKE_CAFFEINATE).sweep(bystander.pid) is False
        assert bystander.poll() is None
    finally:
        bystander.kill()
        bystander.wait()


def test_i5_sweep_spares_a_process_that_only_mentions_a_file_named_like_caffeinate(procs, tmp_path):
    lookalike = tmp_path / "fake_caffeinate.py"
    lookalike.write_text("import time\ntime.sleep(30)\n")
    bystander = procs.spawn(sys.executable, str(lookalike), "-w", "1")
    try:
        assert SubprocessRunner(executable=FAKE_CAFFEINATE).sweep(bystander.pid) is False
        assert bystander.poll() is None
    finally:
        bystander.kill()
        bystander.wait()


def test_i5_sweep_ignores_a_pid_that_no_longer_exists(procs):
    gone = procs.spawn("true")
    gone.wait()
    assert SubprocessRunner(executable=FAKE_CAFFEINATE).sweep(gone.pid) is False


def test_i6_ensure_restarts_a_child_that_died(runner, procs):
    runner.ensure(FLAGS)
    first = started(procs, 1)[0]["pid"]
    os.kill(first, signal.SIGKILL)
    wait_for(lambda: runner.pid is None)

    runner.ensure(FLAGS)

    assert runner.pid not in (None, first) and alive(runner.pid)


def test_i7_missing_executable_raises_and_leaves_nothing_running():
    runner = SubprocessRunner(executable="/nonexistent/caffeinate")
    with pytest.raises(OSError):
        runner.ensure(FLAGS)
    assert runner.pid is None
    runner.shutdown()


def test_i9_ensure_with_the_running_flags_does_nothing(runner, procs):
    runner.ensure(FLAGS)
    pid = runner.pid
    runner.ensure(FLAGS)
    runner.ensure(FLAGS)

    assert runner.pid == pid
    time.sleep(0.2)
    assert len(procs.caffeinates()) == 1


def test_single_instance_lock_is_refused_while_another_holder_has_it(home):
    first = single_instance_lock(home / "app.lock")
    assert first is not None
    assert single_instance_lock(home / "app.lock") is None
    first.close()
    assert single_instance_lock(home / "app.lock") is not None


# --- The reconciler running in its own process (tests/fixtures/headless.py), as the app would ---


@pytest.fixture
def helper(procs):
    """Start the headless reconciler with one active hold; return (process, caffeinate pid)."""

    def start(stale_pid=None):
        conn = db.connect()
        db.put_hold(conn, "s1", "agent", "test", time.time() + 600, time.time())
        proc = procs.spawn(sys.executable, HEADLESS, FAKE_CAFFEINATE)
        pid = wait_for(lambda: (pid := db.get(conn, "caffeinate_pid")) != stale_pid and pid)
        return proc, procs.track(int(pid))

    return start


def test_i1_sigkill_of_the_app_takes_caffeinate_down_with_it(helper, procs):
    proc, caffeinate = helper()
    started(procs, 1)

    proc.kill()
    proc.wait()

    wait_for(lambda: not alive(caffeinate), timeout=2.0)


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_i4_signal_stops_the_child_and_clears_caffeinate_pid(helper, procs, signum):
    proc, caffeinate = helper()
    started(procs, 1)

    proc.send_signal(signum)

    assert proc.wait(timeout=3) == 0
    assert not alive(caffeinate)
    assert db.get(db.connect(), "caffeinate_pid") is None


def test_i4_normal_interpreter_exit_stops_the_child_and_clears_caffeinate_pid(procs):
    conn = db.connect()
    db.put_hold(conn, "s1", "agent", "test", time.time() + 600, time.time())

    proc = procs.spawn(sys.executable, HEADLESS, FAKE_CAFFEINATE, "10")  # ticks 10 times, then just ends

    assert proc.wait(timeout=5) == 0
    (record,) = started(procs, 1)
    assert not alive(record["pid"])
    assert db.get(conn, "caffeinate_pid") is None


def test_i5_a_new_app_sweeps_the_caffeinate_an_old_one_left_behind(helper, procs):
    stale = procs.spawn(FAKE_CAFFEINATE, "-i", "-s", "-w", str(os.getpid()))
    started(procs, 1)
    db.put(db.connect(), "caffeinate_pid", str(stale.pid))

    proc, caffeinate = helper(stale_pid=str(stale.pid))

    assert stale.wait(timeout=3) == -signal.SIGTERM
    assert caffeinate != stale.pid and alive(caffeinate)
    proc.terminate()
    assert proc.wait(timeout=3) == 0


def test_i7_missing_executable_is_handled_and_the_loop_keeps_running():
    conn = db.connect()
    db.put_hold(conn, "s1", "agent", "test", time.time() + 600, time.time())
    rec = Reconciler(SubprocessRunner(executable="/nonexistent/caffeinate"))

    assert rec.tick().status == ERROR
    assert rec.tick().status == ERROR
    db.delete_hold(conn, "s1")
    assert rec.tick().status == "Off"
