#!/usr/bin/env python3
"""Stand-in for /usr/bin/caffeinate in tests: same CLI, no power assertions."""

import json
import os
import signal
import sys
import time

args = sys.argv[1:]
if os.environ.get("FAKE_CAFFEINATE_IGNORE_TERM") == "1":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


record_path = os.path.join(os.environ["MRCOFFEE_HOME"], "spawned")
earlier = [json.loads(line)["pid"] for line in open(record_path)] if os.path.exists(record_path) else []
with open(record_path, "a") as f:
    record = {"pid": os.getpid(), "argv": args, "alive_at_start": [pid for pid in earlier if alive(pid)]}
    f.write(json.dumps(record) + "\n")

watched = int(args[args.index("-w") + 1]) if "-w" in args else None
while True:
    if watched is not None:
        try:
            os.kill(watched, 0)
        except ProcessLookupError:
            sys.exit(0)
    time.sleep(0.05)
