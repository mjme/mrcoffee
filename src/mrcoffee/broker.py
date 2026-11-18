"""Pure logic: holds + settings + now -> what caffeinate should be doing and what the menu shows."""

from dataclasses import dataclass


@dataclass(frozen=True)
class View:
    flags: tuple[str, ...] | None  # caffeinate flags; None means nothing should run
    status: str
    manual: bool = False
    keep_display: bool = False
    agents: int = 0

    @property
    def awake(self) -> bool:
        return self.flags is not None


def is_active(hold, now: float) -> bool:
    return hold["expires_at"] is None or hold["expires_at"] > now


def decide(holds, keep_display: bool, now: float) -> View:
    """The core rule: the Mac is kept awake if and only if at least one hold is active."""
    active = [h for h in holds if is_active(h, now)]
    manual = any(h["kind"] == "manual" for h in active)
    agents = sum(h["kind"] == "agent" for h in active)
    if not active:
        return View(None, "Off", keep_display=keep_display)
    parts = ["manual"] * manual + [f"{agents} agent{'' if agents == 1 else 's'}"] * bool(agents)
    status = "Awake · " + " + ".join(parts) + (" · display on" if keep_display else "")
    flags = ("-d", "-i", "-s") if keep_display else ("-i", "-s")
    return View(flags, status, manual, keep_display, agents)
