import copy
import json
import os
import plistlib
from pathlib import Path

import pytest

from mrcoffee import cli, db, install

EXE = "/opt/tools/bin/mrcoffee"
OURS = {"type": "command", "command": f"{EXE} hook"}
ORIGINAL = {
    "model": "fable",
    "hooks": {
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "echo hi"}]}],
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "audit.sh"}]}],
    },
    "permissions": {"allow": ["Bash(git:*)"], "deny": ["Read(.env)"]},
}
NOTES = "# My notes\n\nAlways use tabs.\n"


@pytest.fixture
def launchctl():
    """A stand-in for launchctl that records calls; tests never run the real one."""

    def fake(*args):
        fake.calls.append(args)
        return fake.results.pop(0) if fake.results else 0

    fake.calls, fake.results = [], []
    return fake


@pytest.fixture
def paths():
    home = Path.home()  # the per-test temp HOME
    (home / ".claude").mkdir()

    class P:
        plist = home / "Library/LaunchAgents/com.mrcoffee.app.plist"
        settings = home / ".claude/settings.json"
        claude_md = home / ".claude/CLAUDE.md"

    return P


def settings(paths):
    return json.loads(paths.settings.read_text())


def backups(paths):
    return sorted(paths.settings.parent.glob("settings.json.bak-*"))


# --- LaunchAgent ---


def test_install_writes_the_launch_agent_plist(paths, launchctl, home):
    install.install(exe=EXE, launchctl=launchctl)

    plist = plistlib.loads(paths.plist.read_bytes())
    assert plist["Label"] == "com.mrcoffee.app"
    assert plist["ProgramArguments"] == [EXE, "app"]
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] == {"SuccessfulExit": False}
    assert plist["StandardOutPath"] == plist["StandardErrorPath"] == str(home / "launchd.log")


def test_install_boots_out_any_loaded_agent_then_bootstraps(paths, launchctl):
    install.install(exe=EXE, launchctl=launchctl)
    domain = f"gui/{os.getuid()}"
    assert launchctl.calls == [
        ("bootout", f"{domain}/com.mrcoffee.app"),
        ("bootstrap", domain, str(paths.plist)),
    ]


def test_install_retries_bootstrap_while_the_old_agent_is_still_unloading(paths, launchctl, monkeypatch):
    monkeypatch.setattr(install.time, "sleep", lambda seconds: None)
    launchctl.results = [0, 5, 5, 0]  # bootout ok, bootstrap fails twice, then succeeds
    summary = install.install(exe=EXE, launchctl=launchctl)
    assert [call[0] for call in launchctl.calls] == ["bootout", "bootstrap", "bootstrap", "bootstrap"]
    assert "started" in "\n".join(summary)


def test_install_reports_when_the_agent_could_not_be_started(paths, launchctl, monkeypatch):
    monkeypatch.setattr(install.time, "sleep", lambda seconds: None)
    launchctl.results = [0] + [5] * 50
    assert "could not start" in "\n".join(install.install(exe=EXE, launchctl=launchctl))


def test_install_creates_the_db(paths, launchctl, home):
    install.install(exe=EXE, launchctl=launchctl)
    assert (home / "holds.db").exists()


def test_no_claude_leaves_the_claude_config_alone(paths, launchctl):
    paths.settings.write_text(json.dumps(ORIGINAL))
    install.install(claude=False, exe=EXE, launchctl=launchctl)
    assert settings(paths) == ORIGINAL
    assert not paths.claude_md.exists() and backups(paths) == []
    assert paths.plist.exists()


# --- ~/.claude/settings.json ---


def test_install_merges_hooks_and_permission_without_clobbering_existing_ones(paths, launchctl):
    paths.settings.write_text(json.dumps(ORIGINAL))
    install.install(exe=EXE, launchctl=launchctl)

    merged = settings(paths)
    assert merged["model"] == "fable"
    assert merged["hooks"]["PreToolUse"] == ORIGINAL["hooks"]["PreToolUse"]
    assert merged["hooks"]["UserPromptSubmit"] == [*ORIGINAL["hooks"]["UserPromptSubmit"], {"hooks": [OURS]}]
    for event in ("Stop", "StopFailure", "SessionEnd"):
        assert merged["hooks"][event] == [{"hooks": [OURS]}]
    assert merged["permissions"] == {"allow": ["Bash(git:*)", "Bash(mrcoffee:*)"], "deny": ["Read(.env)"]}


def test_install_backs_up_settings_before_changing_them(paths, launchctl):
    paths.settings.write_text(json.dumps(ORIGINAL))
    install.install(exe=EXE, launchctl=launchctl)
    (backup,) = backups(paths)
    assert json.loads(backup.read_text()) == ORIGINAL


def test_install_twice_adds_no_duplicates_and_no_second_backup(paths, launchctl):
    paths.settings.write_text(json.dumps(ORIGINAL))
    install.install(exe=EXE, launchctl=launchctl)
    once = settings(paths)
    install.install(exe=EXE, launchctl=launchctl)
    assert settings(paths) == once
    assert len(backups(paths)) == 1


def test_reinstall_from_a_different_path_replaces_the_hook_commands(paths, launchctl):
    install.install(exe="/old/place/mrcoffee", launchctl=launchctl)
    install.install(exe=EXE, launchctl=launchctl)
    assert settings(paths)["hooks"]["Stop"] == [{"hooks": [OURS]}]


def test_install_creates_settings_when_there_are_none(paths, launchctl):
    install.install(exe=EXE, launchctl=launchctl)
    assert settings(paths)["hooks"]["UserPromptSubmit"] == [{"hooks": [OURS]}]
    assert backups(paths) == []


def test_uninstall_restores_the_original_settings(paths, launchctl):
    paths.settings.write_text(json.dumps(ORIGINAL))
    install.install(exe=EXE, launchctl=launchctl)
    install.uninstall(launchctl=launchctl)
    assert settings(paths) == ORIGINAL


def test_uninstall_leaves_no_empty_containers_behind(paths, launchctl):
    paths.settings.write_text(json.dumps({"model": "fable"}))
    install.install(exe=EXE, launchctl=launchctl)
    install.uninstall(launchctl=launchctl)
    assert settings(paths) == {"model": "fable"}


def test_uninstall_keeps_hooks_that_merely_mention_mrcoffee(paths, launchctl):
    theirs = copy.deepcopy(ORIGINAL)
    theirs["hooks"]["Stop"] = [{"hooks": [{"type": "command", "command": "echo mrcoffee hook"}]}]
    paths.settings.write_text(json.dumps(theirs))
    install.install(exe=EXE, launchctl=launchctl)
    install.uninstall(launchctl=launchctl)
    assert settings(paths) == theirs


def test_invalid_settings_json_aborts_before_anything_is_changed(paths, launchctl, home):
    paths.settings.write_text("{ not json")
    with pytest.raises(SystemExit) as exit_info:
        install.install(exe=EXE, launchctl=launchctl)
    assert "left untouched" in str(exit_info.value)
    assert paths.settings.read_text() == "{ not json"
    assert launchctl.calls == [] and not paths.plist.exists() and not paths.claude_md.exists()


@pytest.mark.parametrize(
    "odd",
    [
        '{"hooks": null}',
        '{"hooks": []}',
        '{"hooks": {"Stop": {"not": "a list"}}}',
        '{"hooks": {"Stop": ["not a group"]}}',
        '{"permissions": null}',
        '{"permissions": {"allow": "Bash(git:*)"}}',
        "[1, 2]",
    ],
)
def test_settings_with_an_unexpected_shape_abort_before_anything_is_changed(paths, launchctl, odd):
    paths.settings.write_text(odd)
    with pytest.raises(SystemExit) as exit_info:
        install.install(exe=EXE, launchctl=launchctl)
    assert "left untouched" in str(exit_info.value)
    assert paths.settings.read_text() == odd
    assert launchctl.calls == [] and not paths.plist.exists() and not paths.claude_md.exists()

    try:  # uninstall may refuse too, or find nothing of ours to remove; either way the file is not rewritten
        install.uninstall(launchctl=launchctl)
    except SystemExit:
        assert launchctl.calls == []
    assert paths.settings.read_text() == odd and backups(paths) == []


def test_install_keeps_changes_made_to_settings_while_launchctl_was_running(paths):
    paths.settings.write_text(json.dumps(ORIGINAL))

    def launchctl(*args):  # Claude Code saves a new permission while we wait on launchd
        if args[0] == "bootstrap":
            changed = settings(paths)
            changed["permissions"]["allow"].append("Bash(npm:*)")
            paths.settings.write_text(json.dumps(changed))
        return 0

    install.install(exe=EXE, launchctl=launchctl)

    assert settings(paths)["permissions"]["allow"] == ["Bash(git:*)", "Bash(npm:*)", "Bash(mrcoffee:*)"]


def test_non_ascii_settings_survive_byte_for_byte_whatever_the_locale(paths, launchctl, monkeypatch):
    monkeypatch.setattr("locale.getencoding", lambda: "ISO8859-1")
    monkeypatch.setattr("io.text_encoding", lambda encoding, stacklevel=2: encoding or "ISO8859-1")
    original = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo ☕ café"}]}]}}
    paths.settings.write_bytes(json.dumps(original, ensure_ascii=False).encode("utf-8"))

    install.install(exe=EXE, launchctl=launchctl)

    assert "echo ☕ café" in paths.settings.read_bytes().decode("utf-8")
    install.uninstall(launchctl=launchctl)
    assert json.loads(paths.settings.read_bytes().decode("utf-8")) == original


def test_a_backup_is_never_overwritten_by_a_later_one_in_the_same_second(paths, launchctl, monkeypatch):
    monkeypatch.setattr(install.time, "strftime", lambda fmt: "20260101-000000")
    paths.settings.write_text(json.dumps(ORIGINAL))
    install.install(exe=EXE, launchctl=launchctl)
    install.uninstall(launchctl=launchctl)
    (backup,) = backups(paths)
    assert json.loads(backup.read_text()) == ORIGINAL


def test_a_symlinked_settings_file_stays_a_symlink(paths, launchctl, tmp_path):
    real = tmp_path / "dotfiles-settings.json"
    real.write_text(json.dumps(ORIGINAL))
    paths.settings.symlink_to(real)
    install.install(exe=EXE, launchctl=launchctl)
    assert paths.settings.is_symlink()
    assert json.loads(real.read_text())["hooks"]["Stop"] == [{"hooks": [OURS]}]


# --- ~/.claude/CLAUDE.md ---


def test_install_appends_the_marked_block_to_claude_md(paths, launchctl):
    paths.claude_md.write_text(NOTES)
    install.install(exe=EXE, launchctl=launchctl)
    text = paths.claude_md.read_text()
    assert text.startswith(NOTES + "\n<!-- mrcoffee:start -->\nMr. Coffee keeps this Mac awake.")
    assert text.endswith("shows current status.\n<!-- mrcoffee:end -->\n")
    assert 'mrcoffee acquire --minutes N --reason "<what>"' in text and "mrcoffee release <id>" in text


def test_install_twice_keeps_a_single_block(paths, launchctl):
    paths.claude_md.write_text(NOTES)
    install.install(exe=EXE, launchctl=launchctl)
    once = paths.claude_md.read_text()
    install.install(exe=EXE, launchctl=launchctl)
    assert paths.claude_md.read_text() == once
    assert once.count("<!-- mrcoffee:start -->") == 1


def test_install_replaces_an_outdated_block(paths, launchctl):
    paths.claude_md.write_text(NOTES + "\n<!-- mrcoffee:start -->\nold words\n<!-- mrcoffee:end -->\n")
    install.install(exe=EXE, launchctl=launchctl)
    text = paths.claude_md.read_text()
    assert "old words" not in text and text.count("<!-- mrcoffee:start -->") == 1


def test_uninstall_removes_the_block_and_restores_claude_md(paths, launchctl):
    paths.claude_md.write_text(NOTES)
    install.install(exe=EXE, launchctl=launchctl)
    install.uninstall(launchctl=launchctl)
    assert paths.claude_md.read_text() == NOTES


def test_uninstall_deletes_a_claude_md_that_install_created(paths, launchctl):
    install.install(exe=EXE, launchctl=launchctl)
    assert paths.claude_md.exists()
    install.uninstall(launchctl=launchctl)
    assert not paths.claude_md.exists()


def test_a_damaged_marker_never_costs_the_user_their_own_text(paths, launchctl):
    damaged = "# Notes\n<!-- mrcoffee:start -->\n## My important section\nkeep me\n"
    paths.claude_md.write_text(damaged)
    install.install(exe=EXE, launchctl=launchctl)
    install.install(exe=EXE, launchctl=launchctl)
    assert paths.claude_md.read_text().count("Mr. Coffee keeps this Mac awake") == 1
    install.uninstall(launchctl=launchctl)
    assert paths.claude_md.read_text() == damaged


def test_claude_md_keeps_its_windows_line_endings(paths, launchctl):
    crlf = b"# Notes\r\n\r\nUse tabs.\r\n"
    paths.claude_md.write_bytes(crlf)
    install.install(exe=EXE, launchctl=launchctl)
    assert paths.claude_md.read_bytes().startswith(crlf)
    install.uninstall(launchctl=launchctl)
    assert paths.claude_md.read_bytes() == crlf


def test_a_symlinked_claude_md_is_edited_in_place_and_stays_a_symlink(paths, launchctl, tmp_path):
    real = tmp_path / "dotfiles-CLAUDE.md"
    real.write_text(NOTES)
    paths.claude_md.symlink_to(real)
    install.install(exe=EXE, launchctl=launchctl)
    assert "mrcoffee:start" in real.read_text()
    install.uninstall(launchctl=launchctl)
    assert paths.claude_md.is_symlink() and real.read_text() == NOTES


# --- uninstall ---


def test_uninstall_boots_out_and_deletes_the_plist(paths, launchctl):
    install.install(exe=EXE, launchctl=launchctl)
    launchctl.calls.clear()
    install.uninstall(launchctl=launchctl)
    assert launchctl.calls == [("bootout", f"gui/{os.getuid()}/com.mrcoffee.app")]
    assert not paths.plist.exists()


def test_uninstall_without_purge_keeps_the_db(paths, launchctl, home):
    install.install(exe=EXE, launchctl=launchctl)
    install.uninstall(launchctl=launchctl)
    assert (home / "holds.db").exists()


def test_purge_deletes_the_db_and_logs_but_nothing_else_in_the_folder(paths, launchctl, home):
    install.install(exe=EXE, launchctl=launchctl)
    for name in ("mrcoffee.log", "mrcoffee.log.1", "launchd.log", "app.lock"):
        (home / name).write_text("x")
    (home / "keep-me.txt").write_text("not ours")

    install.uninstall(purge=True, launchctl=launchctl)

    assert sorted(p.name for p in home.iterdir()) == ["keep-me.txt"]


def test_uninstall_when_nothing_is_installed_is_harmless(paths, launchctl):
    install.uninstall(launchctl=launchctl)
    assert not paths.settings.exists() and not paths.claude_md.exists()


# --- CLI wiring ---


def test_cli_install_and_uninstall_pass_their_flags_through(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(install, "install", lambda claude: seen.append(("install", claude)) or ["installed"])
    monkeypatch.setattr(install, "uninstall", lambda purge: seen.append(("uninstall", purge)) or ["removed"])

    assert cli.main(["install", "--no-claude"]) == 0
    assert cli.main(["install"]) == 0
    assert cli.main(["uninstall", "--purge"]) == 0

    assert seen == [("install", False), ("install", True), ("uninstall", True)]
    assert capsys.readouterr().out == "installed\ninstalled\nremoved\n"


def test_the_real_launchctl_is_unreachable_from_tests():
    with pytest.raises(AssertionError):
        install.install(exe=EXE)
