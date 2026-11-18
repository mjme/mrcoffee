"""The `mrcoffee` command. `hook` runs on every Claude Code prompt, so its path imports as little as possible."""

import json
import os
import sys
import time

from . import db

HOOK_HOLD_SECONDS = 4 * 3600
APP_STALE_AFTER = 10.0  # seconds without a heartbeat before the app counts as not running


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["hook"]:  # fast path: skip argparse
        return hook()

    import argparse

    parser = argparse.ArgumentParser(prog="mrcoffee", description="Keep the Mac awake while Claude Code works.")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")
    sub.add_parser("app", help="run the menu bar app (used by the LaunchAgent)")
    sub.add_parser("hook", help="handle a Claude Code hook event read from stdin")
    p = sub.add_parser("acquire", help="hold the Mac awake for a while; prints the hold id")
    p.add_argument("--minutes", type=int, default=60, help="1-480, default 60")
    p.add_argument("--reason", help="what the hold is for")
    p = sub.add_parser("release", help="release a hold by id")
    p.add_argument("id")
    p = sub.add_parser("radar", help="show status")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p = sub.add_parser("install", help="install the LaunchAgent and the Claude Code hooks")
    p.add_argument("--no-claude", action="store_true", help="leave the Claude Code config alone")
    p = sub.add_parser("uninstall", help="remove what install added")
    p.add_argument("--purge", action="store_true", help="also delete the DB and logs")
    args = parser.parse_args(argv)

    if args.command == "app":
        setup_logging()
        from . import app

        return app.main()
    if args.command in ("install", "uninstall"):
        from . import install

        lines = install.install(claude=not args.no_claude) if args.command == "install" else install.uninstall(args.purge)
        print("\n".join(lines))
        return 0
    conn, now = db.connect(), time.time()
    if args.command == "acquire":
        import uuid

        hold_id = str(uuid.uuid4())
        db.put_hold(conn, hold_id, "agent", args.reason, now + min(max(args.minutes, 1), 480) * 60, now)
        if not status(conn, now)["app_running"]:
            print("mrcoffee: warning: the menu bar app isn't running, so this hold has no effect yet", file=sys.stderr)
        print(hold_id)
    elif args.command == "release":
        db.delete_hold(conn, args.id)
    elif args.command == "radar":
        print(json.dumps(status(conn, now), indent=2) if args.json else _describe(status(conn, now)))
    return 0


def hook() -> int:
    """Handle a Claude Code hook event. Never prints, never fails, never blocks Claude."""
    sys.stdout = open(os.devnull, "w")  # UserPromptSubmit stdout would be injected into Claude's context
    try:
        event = json.load(sys.stdin)
        name, session = event.get("hook_event_name"), event.get("session_id")
        if not isinstance(session, str) or session in ("", "manual"):
            pass
        elif name == "UserPromptSubmit":
            now = time.time()
            db.put_hold(db.connect(), session, "agent", "claude session", now + HOOK_HOLD_SECONDS, now)
        elif name in ("Stop", "StopFailure", "SessionEnd"):
            db.delete_hold(db.connect(), session)
    except BaseException:
        try:
            setup_logging().exception("hook failed")
        except BaseException:
            pass
    return 0


def status(conn, now: float) -> dict:
    from . import broker

    app_running = now - float(db.get(conn, "app_heartbeat", "0")) < APP_STALE_AFTER
    pid = db.get(conn, "caffeinate_pid")
    return {
        "awake": app_running and pid is not None,  # what is, not what the holds ask for
        "keep_display": db.get(conn, "keep_display") == "1",
        "app_running": app_running,
        "caffeinate_pid": int(pid) if pid else None,
        "holds": [
            {
                "id": h["id"],
                "kind": h["kind"],
                "reason": h["reason"],
                "remaining_seconds": None if h["expires_at"] is None else round(h["expires_at"] - now),
            }
            for h in db.holds(conn)
            if broker.is_active(h, now)
        ],
    }


def _describe(s: dict) -> str:
    def left(seconds):
        if seconds is None:
            return "no expiry"
        minutes = seconds // 60
        return f"{minutes // 60}h {minutes % 60}m left" if minutes >= 60 else f"{minutes}m left"

    lines = [
        ("Awake" if s["awake"] else "Off") + (" · display on" if s["awake"] and s["keep_display"] else ""),
        f"App: running, caffeinate pid {s['caffeinate_pid'] or 'none'}" if s["app_running"] else "App: not running",
        "Holds:" if s["holds"] else "Holds: no active holds",
    ]
    lines += [f"  {h['id']:36}  {h['kind']:6}  {left(h['remaining_seconds']):12}  {h['reason'] or ''}" for h in s["holds"]]
    if s["holds"] and not s["app_running"]:
        lines.append("Warning: holds are active but the menu bar app isn't running, so the Mac can still sleep.")
    return "\n".join(lines)


def setup_logging():
    import logging.handlers

    path = db.log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=1)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log = logging.getLogger("mrcoffee")
    log.setLevel(logging.INFO)
    log.addHandler(handler)
    log.propagate = False
    return log
