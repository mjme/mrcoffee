import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from mrcoffee import cli, db

MRCOFFEE = str(Path(sys.executable).parent / "mrcoffee")  # the real console script
SESSION = "0b1e7c1e-aaaa-bbbb-cccc-1234567890ab"


def run_hook(stdin: str) -> subprocess.CompletedProcess:
    return subprocess.run([MRCOFFEE, "hook"], input=stdin, capture_output=True, text=True, timeout=10)


def event(name, session_id=SESSION):
    return json.dumps({"hook_event_name": name, "session_id": session_id, "cwd": "/tmp", "prompt": "hi"})


def assert_silent_success(result):
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def hold_ids():
    return [h["id"] for h in db.holds(db.connect())]


# --- mrcoffee hook ---


def test_hook_user_prompt_submit_adds_a_four_hour_agent_hold():
    before = time.time()
    assert_silent_success(run_hook(event("UserPromptSubmit")))

    (hold,) = db.holds(db.connect())
    assert (hold["id"], hold["kind"], hold["reason"]) == (SESSION, "agent", "claude session")
    assert before + 4 * 3600 <= hold["expires_at"] <= time.time() + 4 * 3600


def test_hook_second_prompt_in_a_session_replaces_the_hold():
    run_hook(event("UserPromptSubmit"))
    run_hook(event("UserPromptSubmit"))
    assert hold_ids() == [SESSION]


@pytest.mark.parametrize("name", ["Stop", "StopFailure", "SessionEnd"])
def test_hook_end_of_turn_events_delete_only_that_sessions_hold(name):
    run_hook(event("UserPromptSubmit"))
    run_hook(event("UserPromptSubmit", session_id="other-session"))
    cli.main(["acquire", "--minutes", "5"])

    assert_silent_success(run_hook(event(name)))

    remaining = hold_ids()
    assert SESSION not in remaining and "other-session" in remaining and len(remaining) == 2


def test_hook_ignores_other_events():
    assert_silent_success(run_hook(event("PreToolUse")))
    assert hold_ids() == []


@pytest.mark.parametrize("stdin", ["", "not json {", "[1, 2]", '"text"', '{"hook_event_name": "UserPromptSubmit"}'])
def test_hook_does_nothing_on_malformed_input(stdin):
    assert_silent_success(run_hook(stdin))
    assert hold_ids() == []


def test_hook_cannot_touch_the_manual_hold():
    db.toggle_manual(db.connect())
    assert_silent_success(run_hook(event("Stop", session_id="manual")))
    assert_silent_success(run_hook(event("UserPromptSubmit", session_id="manual")))
    assert [(h["id"], h["kind"]) for h in db.holds(db.connect())] == [("manual", "manual")]


def test_hook_stays_silent_and_exits_zero_on_a_db_error(home, monkeypatch):
    blocker = home / "not-a-directory"
    blocker.write_text("")
    monkeypatch.setenv("MRCOFFEE_HOME", str(blocker))
    assert_silent_success(run_hook(event("UserPromptSubmit")))


def test_hook_logs_errors_to_the_log_file(home):
    run_hook("not json {")
    assert "hook failed" in (home / "mrcoffee.log").read_text()


def test_hook_never_lets_stray_output_reach_stdout(monkeypatch, capsys):
    import io

    monkeypatch.setattr(sys, "stdout", sys.stdout)  # so the hook's redirect is undone after the test
    monkeypatch.setattr(sys, "stdin", io.StringIO(event("UserPromptSubmit")))
    monkeypatch.setattr(db, "put_hold", lambda *args: print("noise that must not reach Claude"))

    assert cli.main(["hook"]) == 0

    assert capsys.readouterr().out == ""


def test_hook_gives_up_quietly_when_the_db_stays_locked():
    blocker = db.connect()
    blocker.execute("BEGIN IMMEDIATE")
    try:
        began = time.perf_counter()
        assert_silent_success(run_hook(event("UserPromptSubmit")))
        assert time.perf_counter() - began < 4
    finally:
        blocker.execute("ROLLBACK")
    assert hold_ids() == []


def test_hook_does_not_retry_for_seconds_when_the_home_folder_is_read_only(home):
    db.connect().close()
    home.chmod(0o500)
    try:
        began = time.perf_counter()
        assert_silent_success(run_hook(event("UserPromptSubmit")))
        assert time.perf_counter() - began < 1
    finally:
        home.chmod(0o700)


def test_hook_finishes_well_under_200_ms():
    def timed():
        began = time.perf_counter()
        run_hook(event("UserPromptSubmit"))
        return time.perf_counter() - began

    assert min(timed() for _ in range(5)) < 0.2


# --- mrcoffee acquire / release ---


def test_acquire_prints_only_the_id_and_adds_an_agent_hold(capsys):
    before = time.time()
    assert cli.main(["acquire", "--minutes", "5", "--reason", "long build"]) == 0

    out = capsys.readouterr().out
    assert out == f"{uuid.UUID(out.strip())}\n"
    (hold,) = db.holds(db.connect())
    assert (hold["id"], hold["kind"], hold["reason"]) == (out.strip(), "agent", "long build")
    assert before + 300 <= hold["expires_at"] <= time.time() + 300


@pytest.mark.parametrize("args, minutes", [([], 60), (["--minutes", "0"], 1), (["--minutes", "9999"], 480)])
def test_acquire_defaults_to_60_minutes_and_clamps_to_1_to_480(args, minutes):
    before = time.time()
    cli.main(["acquire", *args])
    (hold,) = db.holds(db.connect())
    assert before + minutes * 60 <= hold["expires_at"] <= time.time() + minutes * 60


def test_acquire_warns_on_stderr_when_the_app_is_not_running(capsys):
    cli.main(["acquire"])
    assert "isn't running" in capsys.readouterr().err


def test_acquire_warns_when_the_heartbeat_is_older_than_10_seconds(capsys):
    db.put(db.connect(), "app_heartbeat", str(time.time() - 11))
    cli.main(["acquire"])
    assert "isn't running" in capsys.readouterr().err


def test_acquire_is_quiet_on_stderr_when_the_app_heartbeat_is_fresh(capsys):
    db.put(db.connect(), "app_heartbeat", str(time.time()))
    cli.main(["acquire"])
    assert capsys.readouterr().err == ""


def test_release_deletes_the_hold(capsys):
    cli.main(["acquire"])
    hold_id = capsys.readouterr().out.strip()
    assert cli.main(["release", hold_id]) == 0
    assert hold_ids() == []


def test_release_of_an_unknown_id_still_exits_zero():
    assert cli.main(["release", "no-such-id"]) == 0


# --- mrcoffee radar ---


def test_radar_json_reports_holds_app_and_caffeinate(capsys):
    conn = db.connect()
    db.toggle_manual(conn)
    db.put_hold(conn, "s1", "agent", "claude session", time.time() + 600, time.time())
    db.put_hold(conn, "expired", "agent", "old", time.time() - 1, time.time() - 60)
    db.put(conn, "keep_display", "1")
    db.put(conn, "app_heartbeat", str(time.time()))
    db.put(conn, "caffeinate_pid", "4242")

    assert cli.main(["radar", "--json"]) == 0

    status = json.loads(capsys.readouterr().out)
    assert status["awake"] is True
    assert status["keep_display"] is True
    assert status["app_running"] is True
    assert status["caffeinate_pid"] == 4242
    assert [(h["id"], h["kind"], h["reason"]) for h in status["holds"]] == [
        ("manual", "manual", None),
        ("s1", "agent", "claude session"),
    ]
    assert status["holds"][0]["remaining_seconds"] is None
    assert 590 <= status["holds"][1]["remaining_seconds"] <= 600


def test_radar_is_not_awake_when_holds_exist_but_the_app_is_not_running(capsys):
    db.toggle_manual(db.connect())
    cli.main(["radar", "--json"])
    status = json.loads(capsys.readouterr().out)
    assert (status["awake"], status["app_running"], status["caffeinate_pid"]) == (False, False, None)


def test_radar_human_output_for_an_idle_running_app(capsys):
    db.put(db.connect(), "app_heartbeat", str(time.time()))
    cli.main(["radar"])
    out = capsys.readouterr().out
    assert "Off" in out and "App: running" in out and "no active holds" in out


def test_radar_human_output_lists_holds_and_warns_when_the_app_is_down(capsys):
    conn = db.connect()
    db.put_hold(conn, "s1", "agent", "claude session", time.time() + 3 * 3600 + 150, time.time())
    cli.main(["radar"])
    out = capsys.readouterr().out
    assert "App: not running" in out
    assert "s1" in out and "claude session" in out and "3h 2m left" in out
    assert "Mac can still sleep" in out


def test_no_command_prints_usage_and_fails(capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main([])
    assert exit_info.value.code == 2
