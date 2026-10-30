"""
Telling a SKIPPED scheduled run from a merely old one.

WHY THIS IS A TEST. The weekends of 2026-10-03/04 and 10-17/18 had no pipeline
run at all -- the machine was shut down on the Friday evening and booted on the
Monday, and WakeToRun cannot resume from a power-off. On both, Saturday's
published headline sat about 0.77 from where the index settled, for roughly 67
hours. On the five weekends that DID run it sat between 0.0000 and 0.0190 off:
the Saturday pass collects the USDA reports that land overnight, and that is
what takes Friday's number from three quarters of a dollar out to a cent.

_STALE_WARN_HOURS = 20 catches those weekends only from Saturday afternoon. At
09:00 on the Saturday the last run is 19.9 hours old -- inside the threshold --
and the hour-count can do no better, because it cannot distinguish a run that
is PENDING from one that was SKIPPED. Nineteen hours stale at 07:00 on a
Tuesday is a normal overnight gap; nineteen hours stale at 09:00 on a Saturday
is a missed run. Same number, opposite meanings.

THE DECISION TABLE IS THE TEST. Every case below is paired: a state where a run
really was skipped, and the neighbouring state where it was not. The obvious
version of this check -- "has the headline been refreshed since its own sale
day?" -- fires every weekday between midnight and 07:45, and a banner that
fires daily is one nobody reads on the morning it matters. That case is pinned
explicitly.
"""
import re
from datetime import datetime
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
APP = HERE.parent / "app.py"
PORTAL_APP = (HERE.parent.parent / "livestock-portal" / "apps"
              / "cme_feeder_cattle" / "app.py")

MISSED_RUN_AFTER = (8, 30)


def skipped(last_run: str, now: str) -> bool:
    """The condition app.py uses, stated once so the table below is readable."""
    last, n = datetime.fromisoformat(last_run), datetime.fromisoformat(now)
    return last.date() < n.date() and (n.hour, n.minute) >= MISSED_RUN_AFTER


@pytest.mark.parametrize("last_run,now,expected,why", [
    # -- the two real incidents ---------------------------------------------
    ("2026-10-02T13:07", "2026-10-03T09:00", True,
     "Saturday 09:00 after the machine was shut down Friday evening. 19.9h "
     "stale, so the hour-count stays silent; this must not."),
    ("2026-10-16T13:08", "2026-10-17T09:00", True,
     "the second missed weekend, same shape"),
    ("2026-10-02T13:07", "2026-10-04T11:00", True,
     "and still true on the Sunday"),

    # -- the neighbouring states where it must stay quiet --------------------
    ("2026-10-10T07:46", "2026-10-10T08:45", False,
     "a weekend that DID run: Saturday's own 07:45 is recorded, so nothing is "
     "owed. The last run must be SATURDAY's here -- an earlier draft of this "
     "case passed Friday's stamp while describing a weekend that ran, which "
     "is the missed-weekend state wearing the control's label."),
    ("2026-10-02T13:07", "2026-10-03T07:00", False,
     "Saturday before the morning trigger's grace -- the run is PENDING, not "
     "skipped, and calling it skipped at 07:00 would be wrong every day"),
    ("2026-10-05T13:00", "2026-10-06T07:00", False,
     "THE CASE THAT KILLS THE OBVIOUS CHECK. Tuesday 07:00: the headline is "
     "Monday and has not had its overnight completion, which is TRUE but is "
     "true every single weekday. Firing here is a banner nobody reads."),
    ("2026-10-06T07:46", "2026-10-06T09:00", False,
     "this morning's run is in, an hour ago"),
    ("2026-10-06T13:02", "2026-10-06T23:30", False,
     "late evening, both of today's runs recorded"),

    # -- a genuinely missed WEEKDAY morning ----------------------------------
    ("2026-10-05T13:00", "2026-10-06T08:31", True,
     "Tuesday one minute past the grace with nothing recorded today: the "
     "morning run was due and did not happen"),
    ("2026-10-05T13:00", "2026-10-06T08:29", False,
     "...and one minute before it, still pending. The boundary is the point."),
])
def test_the_decision_table(last_run, now, expected, why):
    assert skipped(last_run, now) is expected, why


def test_the_check_is_not_simply_an_hour_count():
    """
    The two must disagree, or this adds nothing. Saturday 09:00 after a missed
    weekend and Tuesday 07:00 on a healthy one are BOTH inside
    _STALE_WARN_HOURS, so the hour-count says nothing about either -- and one
    is a skipped run while the other is a perfectly normal overnight gap.

    An earlier draft asserted the two were "within six minutes" of each other.
    They are 19.9 and 18.0 hours, nearly two hours apart; the claim was decor
    and it was false. What actually matters is only that both sit under the
    threshold, which is what makes the hour-count blind to the difference.
    """
    sat = (datetime.fromisoformat("2026-10-03T09:00")
           - datetime.fromisoformat("2026-10-02T13:07")).total_seconds() / 3600
    tue = (datetime.fromisoformat("2026-10-06T07:00")
           - datetime.fromisoformat("2026-10-05T13:00")).total_seconds() / 3600
    assert sat < 20 and tue < 20, (
        "both must be inside _STALE_WARN_HOURS or this check is redundant: "
        "sat=%.2fh tue=%.2fh" % (sat, tue))
    assert skipped("2026-10-02T13:07", "2026-10-03T09:00") is True
    assert skipped("2026-10-05T13:00", "2026-10-06T07:00") is False


@pytest.mark.parametrize("app", [APP, PORTAL_APP], ids=["standalone", "portal"])
def test_both_dashboards_carry_the_check(app):
    if not app.exists():                                   # pragma: no cover
        pytest.skip("%s not checked out beside this repo" % app)
    src = app.read_text(encoding="utf-8")
    assert "_MISSED_RUN_AFTER = (8, 30)" in src, (
        "%s lost the skipped-run threshold" % app)
    assert "last.date() < now.date()" in src, (
        "%s no longer asks whether anything was recorded TODAY -- an "
        "hour-count alone cannot tell a skipped run from a pending one" % app)
    assert "Today's scheduled run has not happened" in src, (
        "%s lost the banner wording" % app)


def test_the_branch_sits_below_the_alert_and_above_the_warn():
    """
    Order matters: at 43 hours both this and the 30-hour alert are true, and
    the alert is the stronger statement. Checked structurally because an
    elif-chain reordered by hand is exactly the kind of edit that looks
    harmless in a diff.
    """
    src = APP.read_text(encoding="utf-8")
    alert = src.index("hours >= _STALE_ALERT_HOURS")
    missed = src.index("last.date() < now.date()")
    warn = src.index("hours >= _STALE_WARN_HOURS")
    assert alert < missed < warn, (
        "the skipped-run branch must sit between the alert and the warn: "
        "alert=%d missed=%d warn=%d" % (alert, missed, warn))


def test_no_dollar_sign_sneaks_into_the_new_banner():
    """Two dollar signs in one Streamlit string is LaTeX, and this repo has
    paid for that twice. The banner says 'three quarters of a dollar' in words
    for exactly this reason."""
    src = APP.read_text(encoding="utf-8")
    m = re.search(r"Today's scheduled run has not happened.*?\n\s*\)", src, re.S)
    assert m, "the banner moved; re-anchor this guard"
    assert "$" not in m.group(0), "a dollar sign entered the banner text"
