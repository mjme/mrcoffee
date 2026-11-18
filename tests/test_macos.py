"""Layer 3: the real /usr/bin/caffeinate and the real menu bar app (macOS only).

The app tests briefly show a cup icon in the menu bar.
"""

import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import HEADLESS, alive, wait_for
from mrcoffee import db

pytestmark = pytest.mark.macos

MRCOFFEE = str(Path(sys.executable).parent / "mrcoffee")
CAFFEINATE = "/usr/bin/caffeinate"


def command(pid: int) -> str:
    return subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout.strip()


def assertions(pid: int) -> str:
    """The lines of `pmset -g assertions` that belong to this caffeinate process."""
    out = subprocess.run(["pmset", "-g", "assertions"], capture_output=True, text=True).stdout
    return "\n".join(line for line in out.splitlines() if f"pid {pid}(caffeinate)" in line)


def start(procs, *argv):
    """Start a process that reconciles, with a hold already in place; return (process, caffeinate pid)."""
    conn = db.connect()
    db.toggle_manual(conn)
    proc = procs.spawn(*argv)
    pid = procs.track(int(wait_for(lambda: db.get(conn, "caffeinate_pid"), timeout=10)))
    return proc, pid


# --- real caffeinate under the headless reconciler ---


def test_real_caffeinate_holds_an_idle_sleep_assertion(procs):
    proc, pid = start(procs, sys.executable, HEADLESS, CAFFEINATE)
    try:
        assert command(pid) == f"{CAFFEINATE} -i -s -w {proc.pid}"
        held = wait_for(lambda: assertions(pid))
        assert "PreventUserIdleSystemSleep" in held
        assert "PreventUserIdleDisplaySleep" not in held
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_i1_real_caffeinate_and_its_assertion_vanish_when_the_app_is_sigkilled(procs):
    proc, pid = start(procs, sys.executable, HEADLESS, CAFFEINATE)
    wait_for(lambda: assertions(pid))

    proc.kill()
    proc.wait()

    wait_for(lambda: not alive(pid), timeout=2.0)
    assert assertions(pid) == ""


def test_keep_display_adds_a_display_sleep_assertion(procs):
    db.put(db.connect(), "keep_display", "1")
    proc, pid = start(procs, sys.executable, HEADLESS, CAFFEINATE)
    try:
        assert command(pid) == f"{CAFFEINATE} -d -i -s -w {proc.pid}"
        assert "PreventUserIdleDisplaySleep" in wait_for(lambda: assertions(pid))
    finally:
        proc.terminate()
        proc.wait(timeout=5)


# --- the real menu bar app ---


def test_app_runs_caffeinate_for_a_hold_and_exits_zero_on_sigterm(procs):
    proc, pid = start(procs, MRCOFFEE, "app")
    assert command(pid) == f"{CAFFEINATE} -i -s -w {proc.pid}"

    began = time.monotonic()
    proc.send_signal(signal.SIGTERM)

    assert proc.wait(timeout=5) == 0
    assert time.monotonic() - began < 3
    assert not alive(pid)
    assert db.get(db.connect(), "caffeinate_pid") is None


def test_app_reacts_to_db_changes_within_about_two_seconds(procs):
    proc, pid = start(procs, MRCOFFEE, "app")
    try:
        db.toggle_manual(db.connect())  # release the only hold
        wait_for(lambda: not alive(pid), timeout=3.0)
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_second_app_instance_exits_zero_and_leaves_the_first_alone(procs):
    proc, pid = start(procs, MRCOFFEE, "app")
    try:
        second = procs.spawn(MRCOFFEE, "app")
        assert second.wait(timeout=5) == 0
        assert proc.poll() is None and alive(pid)
        assert db.get(db.connect(), "caffeinate_pid") == str(pid)
    finally:
        proc.terminate()
        proc.wait(timeout=5)


# --- the menu itself, built in-process with an in-memory runner ---


@pytest.fixture
def delegate():
    from AppKit import NSApplication, NSStatusBar

    from mrcoffee.app import AppDelegate
    from mrcoffee.reconciler import Reconciler
    from test_reconciler import FakeRunner

    NSApplication.sharedApplication()
    delegate = AppDelegate.alloc().init()
    delegate.reconciler = Reconciler(FakeRunner())
    delegate.applicationDidFinishLaunching_(None)
    yield delegate
    NSStatusBar.systemStatusBar().removeStatusItem_(delegate.item)


def menu_rows(delegate):
    """(title, enabled, checked) per row; separators are '-'."""
    items = delegate.item.menu().itemArray()
    return ["-" if i.isSeparatorItem() else (str(i.title()), bool(i.isEnabled()), bool(i.state())) for i in items]


def test_menu_is_exactly_the_specified_dropdown_and_shows_only_an_icon(delegate):
    assert menu_rows(delegate) == [
        ("Off", False, False),
        "-",
        ("Keep Awake", True, False),
        ("Keep Display On", True, False),
        ("Release Agent Holds", False, False),
        "-",
        ("Quit", True, False),
    ]
    button = delegate.item.button()
    assert str(button.title()) == "" and button.image().isTemplate()


def test_menu_clicks_toggle_holds_and_update_checkmarks_at_once(delegate):
    delegate.toggleManual_(None)
    delegate.toggleDisplay_(None)
    rows = menu_rows(delegate)
    assert rows[0] == ("Awake · manual · display on", False, False)
    assert rows[2:4] == [("Keep Awake", True, True), ("Keep Display On", True, True)]
    assert delegate.reconciler.runner.flags == ("-d", "-i", "-s")

    delegate.toggleManual_(None)
    assert menu_rows(delegate)[0] == ("Off", False, False)


def test_release_agent_holds_is_enabled_only_when_there_are_agent_holds(delegate):
    db.put_hold(db.connect(), "s1", "agent", "x", time.time() + 60, time.time())
    delegate.tick_(None)
    assert menu_rows(delegate)[4] == ("Release Agent Holds", True, False)

    delegate.releaseAgents_(None)

    assert db.holds(db.connect()) == []
    assert menu_rows(delegate)[4] == ("Release Agent Holds", False, False)


def test_icon_is_the_filled_cup_when_awake_and_the_outline_when_off(delegate):
    off = delegate.item.button().image().TIFFRepresentation()
    delegate.toggleManual_(None)
    awake = delegate.item.button().image().TIFFRepresentation()
    assert off != awake


def test_bundled_fallback_icons_load_at_menu_bar_size():
    from mrcoffee.app import bundled_icon

    for name in ("cup.and.saucer", "cup.and.saucer.fill"):
        image = bundled_icon(name)
        assert image.isValid() and tuple(image.size()) == (18, 18)


QUIT_SCRIPT = """
import sys, time
from AppKit import NSApplication
from mrcoffee import db
from mrcoffee.app import AppDelegate
from mrcoffee.reconciler import Reconciler
from mrcoffee.runner import SubprocessRunner

NSApplication.sharedApplication()
delegate = AppDelegate.alloc().init()
delegate.reconciler = Reconciler(SubprocessRunner())
delegate.applicationDidFinishLaunching_(None)
assert db.get(db.connect(), "caffeinate_pid")
delegate.quit_(None)
"""


def test_i4_quit_stops_caffeinate_clears_its_pid_and_exits_zero(procs):
    conn = db.connect()
    db.toggle_manual(conn)

    proc = procs.spawn(sys.executable, "-c", QUIT_SCRIPT)

    assert proc.wait(timeout=10) == 0
    assert db.get(conn, "caffeinate_pid") is None
    assert subprocess.run(["pgrep", "-f", f"caffeinate -i -s -w {proc.pid}"], capture_output=True).stdout == b""
