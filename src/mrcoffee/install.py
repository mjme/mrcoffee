"""install / uninstall: the LaunchAgent, and the hooks, permission and guidance in the Claude Code config.

Both are safe to re-run. Uninstall removes exactly what install added.
"""

import copy
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import db

LABEL = "com.mrcoffee.app"
HOOK_EVENTS = ("UserPromptSubmit", "Stop", "StopFailure", "SessionEnd")
PERMISSION = "Bash(mrcoffee:*)"
START, END = "<!-- mrcoffee:start -->", "<!-- mrcoffee:end -->"
GUIDANCE = f"""{START}
Mr. Coffee keeps this Mac awake. Normal turns are covered automatically by hooks. If you start work
that keeps running after your turn ends (background processes, long builds or test runs), first run
`mrcoffee acquire --minutes N --reason "<what>"` (N = realistic estimate plus margin, max 480) and save
the printed id. Run `mrcoffee release <id>` when you confirm the work has finished. `mrcoffee radar`
shows current status.
{END}
"""
# One complete block. It may not span a second start marker, so a damaged marker can't swallow the user's text.
BLOCK = re.compile(rf"\n?{re.escape(START)}(?:(?!{re.escape(START)}).)*?{re.escape(END)}\n?", re.DOTALL)


def install(claude: bool = True, exe: str | None = None, launchctl=None) -> list[str]:
    exe, launchctl = exe or _executable(), launchctl or _launchctl
    plist, settings_path, claude_md = _paths()

    def add_ours(settings):
        return _with_ours(settings, exe)

    if claude:
        _edit_settings(settings_path, add_ours, check_only=True)  # an unusable settings.json stops us before any change

    db.connect().close()
    done = [f"Database: {db.home() / 'holds.db'}"]

    log = db.log_path().parent / "launchd.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(
        plistlib.dumps(
            {
                "Label": LABEL,
                "ProgramArguments": [exe, "app"],
                "RunAtLoad": True,
                "KeepAlive": {"SuccessfulExit": False},  # restart after a crash, stay quit after Quit
                "StandardOutPath": str(log),
                "StandardErrorPath": str(log),
            }
        )
    )
    domain = f"gui/{os.getuid()}"
    launchctl("bootout", f"{domain}/{LABEL}")  # fails harmlessly when it isn't loaded
    for _ in range(10):  # bootout is asynchronous, so bootstrap can fail for a moment
        if started := launchctl("bootstrap", domain, str(plist)) == 0:
            break
        time.sleep(0.5)
    done.append(f"LaunchAgent: {plist} " + ("written and started" if started else "written, but launchctl could not start it"))

    if claude:
        done.append(f"Claude Code hooks and permission: {_edit_settings(settings_path, add_ours)}")
        done.append("CLAUDE.md: " + ("guidance block written" if _set_block(claude_md, True) else "already up to date"))
    return done


def uninstall(purge: bool = False, launchctl=None) -> list[str]:
    launchctl = launchctl or _launchctl
    plist, settings_path, claude_md = _paths()
    _edit_settings(settings_path, _without_ours, check_only=True)

    launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    plist.unlink(missing_ok=True)
    done = ["LaunchAgent: stopped and removed"]
    if settings_path.exists():
        done.append(f"Claude Code hooks and permission: {_edit_settings(settings_path, _without_ours)}")
    if _set_block(claude_md, False):
        done.append("CLAUDE.md: guidance block removed")
    if purge:
        logs = db.log_path().parent
        ours = [db.home() / name for name in ("holds.db", "holds.db-wal", "holds.db-shm", "app.lock")]
        for path in [*ours, *logs.glob("mrcoffee.log*"), logs / "launchd.log"]:
            path.unlink(missing_ok=True)
        for folder in {db.home(), logs}:
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
        done.append("Database and logs: deleted")
    return done


def _paths() -> tuple[Path, Path, Path]:
    home = Path.home()
    return home / "Library/LaunchAgents" / f"{LABEL}.plist", home / ".claude/settings.json", home / ".claude/CLAUDE.md"


def _executable() -> str:
    argv0 = os.path.abspath(sys.argv[0])
    exe = argv0 if os.path.basename(argv0) == "mrcoffee" else shutil.which("mrcoffee")
    if not exe:
        sys.exit("mrcoffee: can't find the installed `mrcoffee` executable; run `uv tool install` first")
    return exe


def _launchctl(*args: str) -> int:
    return subprocess.run(["/bin/launchctl", *args], capture_output=True).returncode


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", newline="") if path.exists() else ""


def _write(path: Path, text: str) -> None:
    """Replace the file atomically, writing through a symlink instead of replacing the link."""
    target = Path(os.path.realpath(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    temp.write_text(text, encoding="utf-8", newline="")
    if target.exists():
        shutil.copymode(target, temp)
    os.replace(temp, target)


def _edit_settings(path: Path, change, check_only: bool = False) -> str:
    """Read settings.json afresh, apply `change`, and save it with a backup. Exits if that isn't safe."""
    try:
        old = json.loads(_read(path) or "{}")
        new = change(old)
    except (ValueError, AttributeError, TypeError) as exc:
        sys.exit(f"mrcoffee: can't safely edit {path} ({exc!r}), so it was left untouched. Fix it and re-run.")
    if check_only or new == old:
        return "already up to date"
    note = ""
    if path.exists():
        backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        if not backup.exists():  # never overwrite an earlier backup, which is closer to the original
            shutil.copy2(path, backup)
        note = f" (backup: {backup})"
    _write(path, json.dumps(new, indent=2, ensure_ascii=False) + "\n")
    return f"{path} updated{note}"


def _is_ours(hook) -> bool:
    try:
        argv = shlex.split(hook.get("command", ""))
    except ValueError:
        return False
    return len(argv) == 2 and os.path.basename(argv[0]) == "mrcoffee" and argv[1] == "hook"


def _without_ours(settings: dict) -> dict:
    """A copy with our hook entries and permission removed, and any container that emptied pruned."""
    settings = copy.deepcopy(settings)
    hooks = settings.get("hooks", {})
    for event in HOOK_EVENTS:
        groups = []
        for group in hooks.get(event, []):
            kept = [h for h in group.get("hooks", []) if not _is_ours(h)]
            if len(kept) == len(group.get("hooks", [])):
                groups.append(group)
            elif kept:
                groups.append({**group, "hooks": kept})
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    permissions = settings.get("permissions", {})
    if PERMISSION in permissions.get("allow", []):
        permissions["allow"].remove(PERMISSION)
        if not permissions["allow"]:
            del permissions["allow"]
    for key in ("hooks", "permissions"):
        if settings.get(key) == {}:
            del settings[key]
    return settings


def _with_ours(settings: dict, exe: str) -> dict:
    """A copy with exactly one of our hook entries per event, plus the permission."""
    settings = _without_ours(settings)
    ours = {"hooks": [{"type": "command", "command": shlex.join([exe, "hook"])}]}
    for event in HOOK_EVENTS:
        settings.setdefault("hooks", {}).setdefault(event, []).append(copy.deepcopy(ours))
    settings.setdefault("permissions", {}).setdefault("allow", []).append(PERMISSION)
    return settings


def _set_block(path: Path, present: bool) -> bool:
    """Add (or refresh) or remove our marked block in CLAUDE.md. True if the file changed."""
    old = _read(path)
    new = BLOCK.sub("", old)
    if present:
        if new and not new.endswith("\n"):
            new += "\n"
        new += ("\n" if new else "") + GUIDANCE
    if new == old:
        return False
    if new or path.is_symlink():
        _write(path, new)
    else:
        path.unlink()  # install created the file, so uninstall removes it
    return True
