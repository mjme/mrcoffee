"""The SQLite file shared by the menu bar app and the CLI: paths, schema, and all SQL."""

import os
import sqlite3
import time
from pathlib import Path

# One list of statements per schema version; PRAGMA user_version counts how many have been applied.
MIGRATIONS = [
    [
        """CREATE TABLE IF NOT EXISTS holds (
             id         TEXT PRIMARY KEY,
             kind       TEXT NOT NULL CHECK (kind IN ('manual','agent')),
             reason     TEXT,
             created_at REAL NOT NULL,
             expires_at REAL
           )""",
        "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    ],
]


def home() -> Path:
    override = os.environ.get("MRCOFFEE_HOME")
    return Path(override) if override else Path.home() / "Library/Application Support/MrCoffee"


def log_path() -> Path:
    if os.environ.get("MRCOFFEE_HOME"):
        return home() / "mrcoffee.log"
    return Path.home() / "Library/Logs/MrCoffee/mrcoffee.log"


def connect() -> sqlite3.Connection:
    home().mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(home() / "holds.db", isolation_level=None)  # autocommit; BEGIN is explicit
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=2000")
    deadline = time.monotonic() + 2
    while True:  # the first-ever switch to WAL ignores busy_timeout, so retry it by hand
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            break
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or time.monotonic() > deadline:
                raise
            time.sleep(0.01)
    conn.execute("PRAGMA synchronous=NORMAL")
    if conn.execute("PRAGMA user_version").fetchone()[0] < len(MIGRATIONS):
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            version = conn.execute("PRAGMA user_version").fetchone()[0]  # re-read under the lock
            for statements in MIGRATIONS[version:]:
                for statement in statements:
                    conn.execute(statement)
            conn.execute(f"PRAGMA user_version={len(MIGRATIONS)}")
    return conn


def holds(conn) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM holds ORDER BY created_at").fetchall()


def put_hold(conn, id: str, kind: str, reason: str | None, expires_at: float | None, now: float) -> None:
    conn.execute("INSERT OR REPLACE INTO holds VALUES (?, ?, ?, ?, ?)", (id, kind, reason, now, expires_at))


def delete_hold(conn, id: str) -> None:
    conn.execute("DELETE FROM holds WHERE id = ?", (id,))


def release_agents(conn) -> None:
    conn.execute("DELETE FROM holds WHERE kind = 'agent'")


def toggle_manual(conn) -> None:
    if conn.execute("DELETE FROM holds WHERE id = 'manual'").rowcount == 0:
        put_hold(conn, "manual", "manual", None, None, time.time())  # no expiry, on purpose


def get(conn, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def put(conn, key: str, value: str | None) -> None:
    if value is None:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
    else:
        conn.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, value))


def toggle_keep_display(conn) -> None:
    put(conn, "keep_display", "0" if get(conn, "keep_display") == "1" else "1")


def snapshot(conn, now: float) -> tuple[list[sqlite3.Row], bool]:
    """One reconcile tick's DB work in a single short transaction."""
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM holds WHERE expires_at <= ?", (now,))
        put(conn, "app_heartbeat", str(now))
        return holds(conn), get(conn, "keep_display") == "1"
