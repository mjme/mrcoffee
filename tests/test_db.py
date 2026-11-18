import sqlite3
import threading

from mrcoffee import db


def test_connect_creates_schema_and_sets_user_version(home):
    conn = db.connect()
    assert (home / "holds.db").exists()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"holds", "settings"} <= tables


def test_connect_creates_missing_home_directory(home, monkeypatch):
    monkeypatch.setenv("MRCOFFEE_HOME", str(home / "deeper" / "still"))
    db.connect()
    assert (home / "deeper" / "still" / "holds.db").exists()


def test_migration_upgrades_existing_db_and_keeps_rows(monkeypatch):
    conn = db.connect()
    db.put_hold(conn, "manual", "manual", None, None, now=1.0)
    conn.close()
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS + [["ALTER TABLE holds ADD COLUMN note TEXT"]])

    conn = db.connect()

    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    assert [tuple(r) for r in conn.execute("SELECT id, note FROM holds")] == [("manual", None)]


def test_migration_from_empty_version_zero_file(home):
    sqlite3.connect(home / "holds.db").close()  # a v0 file with no tables
    conn = db.connect()
    assert conn.execute("SELECT count(*) FROM holds").fetchone()[0] == 0


def test_two_connections_writing_concurrently_never_see_database_is_locked():
    errors = []

    def writer(name):
        try:
            conn = db.connect()
            for i in range(150):
                db.put_hold(conn, f"{name}-{i}", "agent", "x", 2.0, now=1.0)
                db.put(conn, "keep_display", str(i % 2))
                db.delete_hold(conn, f"{name}-{i - 1}")
        except Exception as exc:  # collected and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert sorted(h["id"] for h in db.holds(db.connect())) == ["a-149", "b-149"]


def test_two_connections_opening_a_brand_new_db_at_once_both_succeed(home, monkeypatch):
    # The first-ever switch to WAL can report "database is locked" without honouring busy_timeout.
    for trial in range(100):
        monkeypatch.setenv("MRCOFFEE_HOME", str(home / f"fresh-{trial}"))
        barrier, errors = threading.Barrier(2), []

        def opener():
            barrier.wait()
            try:
                db.connect().close()
            except Exception as exc:  # collected and asserted below
                errors.append(exc)

        threads = [threading.Thread(target=opener) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == [], f"trial {trial}"


def test_settings_get_put_and_delete():
    conn = db.connect()
    assert db.get(conn, "keep_display", "0") == "0"
    db.put(conn, "keep_display", "1")
    assert db.get(conn, "keep_display") == "1"
    db.put(conn, "keep_display", None)
    assert db.get(conn, "keep_display") is None


def test_put_hold_replaces_existing_row():
    conn = db.connect()
    db.put_hold(conn, "s1", "agent", "first", 10.0, now=1.0)
    db.put_hold(conn, "s1", "agent", "second", 20.0, now=2.0)
    assert [(h["reason"], h["expires_at"]) for h in db.holds(conn)] == [("second", 20.0)]


def test_toggle_manual_adds_then_removes_a_hold_with_no_expiry():
    conn = db.connect()
    db.toggle_manual(conn)
    assert [(h["id"], h["kind"], h["expires_at"]) for h in db.holds(conn)] == [("manual", "manual", None)]
    db.toggle_manual(conn)
    assert db.holds(conn) == []


def test_toggle_keep_display_flips_the_setting():
    conn = db.connect()
    db.toggle_keep_display(conn)
    assert db.get(conn, "keep_display") == "1"
    db.toggle_keep_display(conn)
    assert db.get(conn, "keep_display") == "0"


def test_release_agents_leaves_the_manual_hold():
    conn = db.connect()
    db.toggle_manual(conn)
    db.put_hold(conn, "s1", "agent", "x", 99.0, now=1.0)
    db.release_agents(conn)
    assert [h["id"] for h in db.holds(conn)] == ["manual"]


def test_snapshot_deletes_expired_reads_state_and_writes_heartbeat():
    conn = db.connect()
    db.put_hold(conn, "old", "agent", "x", 50.0, now=1.0)
    db.put_hold(conn, "new", "agent", "x", 150.0, now=2.0)
    db.put(conn, "keep_display", "1")

    holds, keep_display = db.snapshot(conn, now=100.0)

    assert [h["id"] for h in holds] == ["new"]
    assert keep_display is True
    assert not conn.in_transaction
    other = db.connect()  # committed, so a second connection sees it
    assert [h["id"] for h in db.holds(other)] == ["new"]
    assert db.get(other, "app_heartbeat") == "100.0"
