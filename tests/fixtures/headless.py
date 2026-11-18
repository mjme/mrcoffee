"""Test-only: run the reconciler with no UI, ticking every 50 ms.

Usage: headless.py <caffeinate> [ticks]. With a tick count it then exits normally.
"""

import sys
import time

from mrcoffee.reconciler import Reconciler
from mrcoffee.runner import SubprocessRunner, install_exit_handlers

reconciler = Reconciler(SubprocessRunner(executable=sys.argv[1], term_timeout=0.5))
reconciler.sweep()
install_exit_handlers(reconciler.shutdown)
ticks = int(sys.argv[2]) if len(sys.argv) > 2 else None
while ticks is None or ticks > 0:
    reconciler.tick()
    time.sleep(0.05)
    ticks = ticks and ticks - 1
