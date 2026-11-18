import pytest

from mrcoffee import broker

NOW = 1000.0
MANUAL = {"kind": "manual", "expires_at": None}


def agent(expires_at=NOW + 60):
    return {"kind": "agent", "expires_at": expires_at}


def test_no_holds_means_nothing_runs():
    view = broker.decide([], keep_display=False, now=NOW)
    assert view.flags is None
    assert not view.awake
    assert view.status == "Off"


def test_expired_agent_hold_does_not_count():
    view = broker.decide([agent(expires_at=NOW - 1)], keep_display=False, now=NOW)
    assert view.flags is None
    assert view.agents == 0


def test_hold_expiring_exactly_now_is_inactive():
    assert not broker.is_active(agent(expires_at=NOW), NOW)


def test_manual_hold_never_expires():
    assert broker.is_active(MANUAL, NOW + 10 * 365 * 86400)


def test_flags_without_keep_display():
    assert broker.decide([MANUAL], keep_display=False, now=NOW).flags == ("-i", "-s")


def test_flags_with_keep_display():
    assert broker.decide([MANUAL], keep_display=True, now=NOW).flags == ("-d", "-i", "-s")


@pytest.mark.parametrize(
    "holds, keep_display, status",
    [
        ([], False, "Off"),
        ([], True, "Off"),
        ([MANUAL], False, "Awake · manual"),
        ([agent()], False, "Awake · 1 agent"),
        ([agent(), agent(), agent()], False, "Awake · 3 agents"),
        ([MANUAL, agent(), agent()], False, "Awake · manual + 2 agents"),
        ([MANUAL], True, "Awake · manual · display on"),
        ([agent(), agent(expires_at=NOW - 5)], True, "Awake · 1 agent · display on"),
    ],
)
def test_status_line(holds, keep_display, status):
    assert broker.decide(holds, keep_display, NOW).status == status


def test_view_reports_checkmarks():
    view = broker.decide([MANUAL, agent()], keep_display=True, now=NOW)
    assert (view.manual, view.keep_display, view.agents) == (True, True, 1)
    view = broker.decide([agent()], keep_display=False, now=NOW)
    assert (view.manual, view.keep_display, view.agents) == (False, False, 1)
