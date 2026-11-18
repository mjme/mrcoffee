"""Layer 1: the reconcile loop with an in-memory runner and an injected clock (no processes)."""

import sqlite3

import pytest

from mrcoffee import db
from mrcoffee.reconciler import ERROR, Reconciler

FLAGS = ("-i", "-s")
DISPLAY_FLAGS = ("-d", "-i", "-s")


class FakeRunner:
    """Records ensure() calls; can be told to raise or to report its child dead."""

    def __init__(self):
        self.calls = []
        self.swept = []
        self.pid = None
        self.flags = None
        self.fail = None
        self._last_pid = 100

    def ensure(self, flags):
        self.calls.append(flags)
        if self.fail:
            raise self.fail
        if flags is None:
            self.pid = None
        elif self.pid is None or flags != self.flags:
            self._last_pid += 1
            self.pid = self._last_pid
        self.flags = flags

    def die(self):
        self.pid = None

    def sweep(self, pid):
        self.swept.append(pid)
        return True

    def shutdown(self):
        self.pid = self.flags = None


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def runner():
    return FakeRunner()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def rec(runner, clock):
    return Reconciler(runner, clock)


@pytest.fixture
def conn():
    return db.connect()


def hold(conn, clock, id="s1", seconds=60):
    db.put_hold(conn, id, "agent", "test", clock.now + seconds, clock.now)


def break_db(monkeypatch):
    def broken(conn, now):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(db, "snapshot", broken)


def test_nothing_runs_without_holds(rec, runner):
    view = rec.tick()
    assert runner.calls == [None]
    assert (view.awake, view.status) == (False, "Off")


def test_starts_when_the_first_hold_appears(rec, runner, conn, clock):
    rec.tick()
    hold(conn, clock)
    view = rec.tick()
    assert runner.calls == [None, FLAGS]
    assert (view.awake, view.status) == (True, "Awake · 1 agent")


def test_stops_when_the_last_hold_is_removed(rec, runner, conn, clock):
    hold(conn, clock)
    rec.tick()
    db.delete_hold(conn, "s1")
    view = rec.tick()
    assert runner.calls == [FLAGS, None]
    assert not view.awake


def test_stops_when_the_last_hold_expires_and_deletes_it(rec, runner, conn, clock):
    hold(conn, clock, seconds=60)
    rec.tick()
    clock.now += 61
    view = rec.tick()
    assert runner.calls == [FLAGS, None]
    assert not view.awake
    assert db.holds(conn) == []


def test_i9_steady_state_asks_for_the_same_flags_and_keeps_the_pid(rec, runner, conn, clock):
    hold(conn, clock)
    rec.tick()
    pid = runner.pid
    rec.tick()
    rec.tick()
    assert runner.calls == [FLAGS, FLAGS, FLAGS]
    assert runner.pid == pid


def test_flags_change_when_keep_display_is_toggled(rec, runner, conn, clock):
    hold(conn, clock)
    rec.tick()
    db.toggle_keep_display(conn)
    view = rec.tick()
    assert runner.calls == [FLAGS, DISPLAY_FLAGS]
    assert view.status == "Awake · 1 agent · display on"


def test_i5_caffeinate_pid_is_written_on_start_and_cleared_on_stop(rec, runner, conn, clock):
    hold(conn, clock)
    rec.tick()
    assert db.get(conn, "caffeinate_pid") == str(runner.pid)
    db.delete_hold(conn, "s1")
    rec.tick()
    assert db.get(conn, "caffeinate_pid") is None


def test_i5_sweep_passes_the_recorded_pid_to_the_runner_and_clears_it(rec, runner, conn):
    db.put(conn, "caffeinate_pid", "4242")
    rec.sweep()
    assert runner.swept == [4242]
    assert db.get(conn, "caffeinate_pid") is None


def test_i5_sweep_does_nothing_without_a_recorded_pid(rec, runner):
    rec.sweep()
    assert runner.swept == []


def test_i6_child_that_died_is_restarted_on_the_next_tick(rec, runner, conn, clock):
    hold(conn, clock)
    rec.tick()
    first = runner.pid
    runner.die()
    view = rec.tick()
    assert runner.pid not in (None, first)
    assert db.get(conn, "caffeinate_pid") == str(runner.pid)
    assert view.awake


def test_i7_runner_exception_gives_error_view_and_the_loop_recovers(rec, runner, conn, clock):
    hold(conn, clock)
    runner.fail = OSError("spawn failed")
    view = rec.tick()
    assert (view.status, view.awake) == (ERROR, False)

    runner.fail = None
    view = rec.tick()
    assert (view.status, view.awake) == ("Awake · 1 agent", True)


def test_i7_db_exception_gives_error_view_and_the_loop_recovers(rec, runner, conn, clock, monkeypatch):
    hold(conn, clock)
    rec.tick()
    with monkeypatch.context() as patch:
        break_db(patch)
        view = rec.tick()
        assert view.status == ERROR
    assert rec.tick().status == "Awake · 1 agent"


def test_i7_a_malformed_hold_row_gives_the_error_view_instead_of_raising(rec, runner, conn, clock):
    hold(conn, clock)
    rec.tick()
    conn.execute("INSERT INTO holds VALUES ('bad', 'agent', NULL, 0, 'soon')")  # another writer's garbage

    view = rec.tick()

    assert (view.status, view.awake) == (ERROR, True)  # still awake: the last good state is kept
    assert rec.act(db.release_agents).status == "Off"  # and the menu can clear it


def test_i4_shutdown_clears_caffeinate_pid_even_if_a_signal_landed_mid_transaction(rec, runner, conn, clock):
    hold(conn, clock)
    rec.tick()
    rec._db().execute("BEGIN IMMEDIATE")  # as if the signal handler interrupted a tick

    rec.shutdown()

    assert db.get(conn, "caffeinate_pid") is None


def test_i7_error_status_is_the_documented_text():
    assert ERROR == "Error – see log"


def test_i8_transient_db_failure_keeps_the_current_state(rec, runner, conn, clock, monkeypatch):
    hold(conn, clock)
    rec.tick()
    break_db(monkeypatch)
    for _ in range(3):
        clock.now += 9.9
        view = rec.tick()
    assert runner.calls == [FLAGS]  # 29.7 s of failures: untouched
    assert view.awake and runner.pid is not None


def test_i8_thirty_seconds_of_db_failures_stops_caffeinate_until_reads_recover(
    rec, runner, conn, clock, monkeypatch
):
    hold(conn, clock, seconds=3600)
    rec.tick()
    with monkeypatch.context() as patch:
        break_db(patch)
        rec.tick()  # first failure
        clock.now += 30
        view = rec.tick()
        assert runner.calls == [FLAGS, None]
        assert (view.status, view.awake) == (ERROR, False)
    view = rec.tick()
    assert runner.calls == [FLAGS, None, FLAGS]
    assert view.awake


def test_i8_failure_window_restarts_after_a_successful_read(rec, runner, conn, clock, monkeypatch):
    hold(conn, clock, seconds=3600)
    with monkeypatch.context() as patch:
        break_db(patch)
        rec.tick()
        clock.now += 20
    rec.tick()  # success resets the window
    with monkeypatch.context() as patch:
        break_db(patch)
        clock.now += 20
        rec.tick()
    assert None not in runner.calls


def test_i4_shutdown_stops_the_runner_and_clears_caffeinate_pid(rec, runner, conn, clock):
    hold(conn, clock)
    rec.tick()
    rec.shutdown()
    rec.shutdown()
    assert runner.pid is None
    assert db.get(conn, "caffeinate_pid") is None


def test_act_applies_a_menu_action_and_returns_the_fresh_view(rec, runner):
    view = rec.act(db.toggle_manual)
    assert (view.status, view.manual) == ("Awake · manual", True)
    assert runner.calls == [FLAGS]
    assert rec.act(db.toggle_manual).status == "Off"


def test_i7_act_survives_a_failing_action(rec):
    def boom(conn):
        raise sqlite3.OperationalError("locked")

    assert rec.act(boom).status == "Off"
