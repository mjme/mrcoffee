"""Everything that touches processes: the caffeinate child, the stale-process sweep, exit handling.

The safety net is `-w <our pid>`: caffeinate itself exits when this process dies, however it dies.
The rest keeps things tidy while we are alive.
"""

import atexit
import fcntl
import logging
import os
import signal
import subprocess
import sys
from typing import Protocol

log = logging.getLogger("mrcoffee")


class CaffeinateRunner(Protocol):
    @property
    def pid(self) -> int | None: ...  # the live child, if any
    def ensure(self, flags: tuple[str, ...] | None) -> None: ...  # None = nothing should run
    def sweep(self, pid: int) -> bool: ...  # kill a caffeinate left by an earlier app
    def shutdown(self) -> None: ...  # idempotent; safe from atexit and signal handlers


class SubprocessRunner:
    def __init__(self, executable: str = "/usr/bin/caffeinate", term_timeout: float = 2.0):
        self.executable = executable
        self.term_timeout = term_timeout
        self._proc: subprocess.Popen | None = None
        self._flags: tuple[str, ...] | None = None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc and self._proc.poll() is None else None

    def ensure(self, flags: tuple[str, ...] | None) -> None:
        if self._proc and self._proc.poll() is not None:  # I6: the child died behind our back
            log.warning("caffeinate pid=%d died unexpectedly (exit %s)", self._proc.pid, self._proc.returncode)
            self._proc = self._flags = None
        if flags == self._flags:  # I9: already in the desired state
            return
        self.shutdown()  # I2: the old child is gone before a new one starts
        if flags is not None:
            args = (self.executable, *flags, "-w", str(os.getpid()))  # I1: never orphaned
            self._proc = subprocess.Popen(
                args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True
            )
            self._flags = flags
            log.info("started caffeinate pid=%d args=%s", self._proc.pid, " ".join(args[1:]))

    def shutdown(self) -> None:
        proc, self._proc, self._flags = self._proc, None, None
        if proc is None:
            return
        if proc.poll() is None:  # I3: SIGTERM, then SIGKILL, and always reap
            proc.terminate()
            try:
                proc.wait(self.term_timeout)
            except subprocess.TimeoutExpired:
                log.warning("caffeinate pid=%d ignored SIGTERM; sending SIGKILL", proc.pid)
                proc.kill()
        proc.wait()
        log.info("stopped caffeinate pid=%d", proc.pid)

    def sweep(self, pid: int) -> bool:
        """I5: terminate `pid` if it really is a caffeinate (and not a reused pid). True if it was."""
        ps = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True)
        argv = ps.stdout.split()
        # Our executable, started the way we start it. (It is argv[1] when the executable is a script.)
        if self.executable not in argv[:2] or "-w" not in argv:
            return False
        log.info("sweeping stale caffeinate pid=%d", pid)
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass  # it exited on its own in the meantime
        return True


def install_exit_handlers(shutdown, exit=lambda: sys.exit(0)) -> None:
    """I4: normal interpreter exit, SIGTERM and SIGINT all run `shutdown`."""
    atexit.register(shutdown)

    def handler(signum, frame):
        log.info("received signal %d; shutting down", signum)
        shutdown()
        exit()

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def single_instance_lock(path):
    """Return an open, locked file, or None if another process already holds the lock."""
    lock = open(path, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        return None
    return lock
