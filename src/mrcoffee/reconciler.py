"""The reconcile loop: DB -> broker -> runner. No UI imports; the app calls tick() every 2 seconds."""

import logging
import time
from dataclasses import replace

from . import broker, db
from .broker import View
from .runner import CaffeinateRunner

log = logging.getLogger("mrcoffee")

ERROR = "Error – see log"
FAIL_TOWARD_SLEEP_AFTER = 30.0  # seconds of consecutive DB failures before caffeinate is stopped (I8)


class Reconciler:
    def __init__(self, runner: CaffeinateRunner, clock=time.time):
        self.runner = runner
        self.clock = clock
        self.view = View(None, "Off")  # the last view computed from a successful read
        self._conn = None
        self._recorded_pid = None  # what settings.caffeinate_pid currently says
        self._failing_since = None
        self._last_error = None

    def sweep(self) -> None:
        """I5: on app start, terminate the caffeinate an earlier app recorded, then clear the record."""
        try:
            if pid := db.get(self._db(), "caffeinate_pid"):
                self.runner.sweep(int(pid))
                db.put(self._db(), "caffeinate_pid", None)
        except Exception as exc:
            self._failed("startup sweep", exc)

    def tick(self) -> View:
        """One reconcile pass. Never raises (I7); returns what the menu should show."""
        now = self.clock()
        try:
            holds, keep_display = db.snapshot(self._db(), now)
        except Exception as exc:
            self._failed("reading the DB", exc)
            self._conn = None  # reconnect next tick
            if self._failing_since is None:
                self._failing_since = now
            if now - self._failing_since >= FAIL_TOWARD_SLEEP_AFTER:  # I8: fail toward sleep
                try:
                    self.runner.ensure(None)
                except Exception as stop_exc:
                    self._failed("stopping caffeinate", stop_exc)
            return self._error_view()
        self._failing_since = None
        try:
            view = broker.decide(holds, keep_display, now)
            self.runner.ensure(view.flags)
            self._record_pid()
        except Exception as exc:
            self._failed("reconciling", exc)
            return self._error_view()
        self.view = view
        if self._last_error:
            log.info("recovered")
            self._last_error = None
        return self.view

    def act(self, action) -> View:
        """Apply a menu action (a db function taking the connection), then reconcile at once."""
        try:
            action(self._db())
        except Exception as exc:
            self._failed("menu action", exc)
        return self.tick()

    def shutdown(self) -> None:
        """I4: stop caffeinate and clear its recorded pid. Idempotent."""
        self.runner.shutdown()
        try:
            conn = self._db()
            if conn.in_transaction:  # a signal can land in the middle of a tick
                conn.rollback()
            db.put(conn, "caffeinate_pid", None)
            self._recorded_pid = None
        except Exception as exc:
            self._failed("clearing caffeinate_pid", exc)

    def _db(self):
        if self._conn is None:
            self._conn = db.connect()
        return self._conn

    def _record_pid(self) -> None:
        pid = self.runner.pid
        if pid != self._recorded_pid:
            db.put(self._db(), "caffeinate_pid", None if pid is None else str(pid))
            self._recorded_pid = pid

    def _error_view(self) -> View:
        return replace(self.view, flags=self.view.flags if self.runner.pid else None, status=ERROR)

    def _failed(self, what: str, exc: Exception) -> None:
        error = f"{what}: {exc!r}"
        if error != self._last_error:  # log each distinct failure once, not every tick
            log.error("%s", error, exc_info=exc)
            self._last_error = error
