"""
When CME owes us a print, and when they merely have not got to it yet.

WHY THIS IS A TEST. backfill_ftp.py counts a 550 from the server as "no file
(weekend/holiday/not yet published)" and exits 0. That is right for a Saturday
and wrong for a print that was due yesterday and never came, and nothing
anywhere told the two apart -- so on 2026-10-21 the 14:15 pull caught nothing
and logged "CME print pull OK - the published print and scorecard are current",
the identical sentence it had logged on 2026-10-20 after catching a real print.

The whole value of publication_status() is that it can say something false.
Every case below is therefore paired: a state where CME IS behind, and the
neighbouring state where it is not, because a guard that fires on a Saturday
morning is one that gets switched off.

The clock is CENTRAL and the band (13:04-15:05) is measured off CME's own MDTM
stamps. scripts/cme_pull.ps1 records the five-hour error that came of reading
those as UTC; nothing here may be re-derived from our ingest logs, which record
when we looked rather than when CME published.

TWO GATES, AND THE AFTERNOON MOVED. index_dates carries PUBLISH_EARLIEST
(13:04, "could exist" -- what catch_up_dates will ASK for) and PUBLISH_OVERDUE
(16:05, "should exist" -- what publication_status calls LATE). They were one
constant at 14:05 until 2026-10-30, which is how its value drifted: moving it
to fix either job silently broke the other.

So a 14:15 in this file used to mean "past the deadline" and now means an
ordinary afternoon. That is not the tests being bent to fit the code -- it is
the correction. Measured over 159 business-day index dates, 42.1% of CME's
prints land after 14:04, so the old clock called two afternoons in five late.
The cost, stated rather than hidden: no scheduled run falls after 16:05, so a
genuinely missing print is now reported by the next morning's 07:45 rather
than the same afternoon's pull. CME's file is a scorecard and not an input to
the index, which is what makes that trade payable.
"""
from datetime import date, datetime

import pytest

import cme_ftp


def d(s):
    return date.fromisoformat(s)


def dt(s):
    return datetime.fromisoformat(s)


def overdue(series_end, now):
    return [x.isoformat() for x in
            cme_ftp.publication_status(d(series_end), dt(now))["overdue"]]


class TestTheMorningThatProducedThis:
    def test_the_2026_10_21_pull_at_1415_cannot_yet_call_it_late(self):
        """
        The live state at 14:15 on 2026-10-21. We held through 10-19 and no
        FC261020.txt ever appeared -- but at 14:15 that is not yet news, and
        saying so would be the false alarm that gets a guard switched off.

        MDTM says 10-20 was eventually posted on 10-22 at 20:48, so CME really
        was late. The point is that 14:15 could not know it: 42.1% of ordinary
        prints land after 14:04.
        """
        assert overdue("2026-10-19", "2026-10-21T14:15") == []

    def test_but_by_1615_the_same_day_it_is_owed(self):
        """The other half of the pair -- the hour at which absence is news."""
        assert overdue("2026-10-19", "2026-10-21T16:15") == ["2026-10-20"]

    def test_the_2026_10_20_pull_the_day_before_was_genuinely_current(self):
        """
        The control that makes the case above mean something. The SAME job, the
        SAME sentence in the log, one day earlier -- and here it was true.
        """
        assert overdue("2026-10-19", "2026-10-20T14:15") == []

    def test_by_the_next_afternoon_two_prints_are_owed(self):
        assert overdue("2026-10-19", "2026-10-22T16:15") == ["2026-10-20",
                                                             "2026-10-21"]


class TestTheDeadlineWithinTheDay:
    """
    CME publishes index date D on the next business day, between 13:04 and
    15:05 Central for an ordinary print. Before PUBLISH_OVERDUE on that day,
    D is not late -- it is pending.
    """

    def test_before_the_deadline_todays_due_print_is_not_yet_owed(self):
        assert overdue("2026-10-20", "2026-10-22T13:00") == []

    def test_a_mid_afternoon_print_is_still_merely_pending(self):
        """15:00 is INSIDE the measured band -- p90 is 15:05. The old 14:05
        clock called this late, and it is the commonest way the banner was
        wrong."""
        assert overdue("2026-10-20", "2026-10-22T15:00") == []

    def test_after_the_deadline_it_is(self):
        assert overdue("2026-10-20", "2026-10-22T16:15") == ["2026-10-21"]

    def test_the_boundary_is_1605_exactly(self):
        assert overdue("2026-10-20", "2026-10-22T16:04") == []
        assert overdue("2026-10-20", "2026-10-22T16:05") == ["2026-10-21"]


class TestWeekendsAndHolidays:
    def test_a_saturday_morning_owes_nothing_extra(self):
        """
        The false alarm that would get this switched off. Read on Saturday
        10-24 holding through Thursday 10-22: Friday 10-23's print is due on
        Monday, and Saturday and Sunday have no index at all.
        """
        assert overdue("2026-10-22", "2026-10-24T09:00") == []

    def test_a_sunday_is_the_same(self):
        assert overdue("2026-10-22", "2026-10-25T09:00") == []

    def test_monday_afternoon_owes_fridays_print_and_no_weekend_dates(self):
        got = overdue("2026-10-22", "2026-10-26T16:15")
        assert got == ["2026-10-23"], (
            "a weekend date was counted as owed, or Friday's was missed: %s"
            % got)

    def test_a_holiday_is_NOT_skipped(self):
        """
        CME publishes on exchange holidays -- 2026-09-07 was Labor Day and
        their file carries an index value with same-day head 0. A holiday
        calendar here would quietly forgive a genuinely missing print.
        """
        assert overdue("2026-09-04", "2026-09-08T16:15") == ["2026-09-07"]


class TestDueThroughItself:
    def test_it_is_never_today(self):
        """
        Today's own index date cannot be published until tomorrow, so it can
        never be owed. A rule that let it be would fire every afternoon.
        """
        for now in ("2026-10-22T14:15", "2026-10-22T23:59", "2026-10-26T15:00"):
            assert cme_ftp.due_through(dt(now)) < dt(now).date()

    def test_it_is_never_a_weekend(self):
        for now in ("2026-10-24T14:15", "2026-10-25T14:15", "2026-10-26T14:15"):
            assert cme_ftp.due_through(dt(now)).weekday() < 5

    def test_a_long_weekend_walks_back_past_it(self):
        """Monday morning, before the deadline: Thursday is the newest owed."""
        assert cme_ftp.due_through(dt("2026-10-26T09:00")) == d("2026-10-22")


class TestDegradation:
    def test_an_empty_series_reports_no_overdue_rather_than_everything(self):
        """
        A fresh backend holds nothing. Reporting every business day since 2015
        as owed would be true and useless, and would bury the one line that
        matters on the morning it appears.
        """
        s = cme_ftp.publication_status(None, dt("2026-10-22T16:15"))
        assert s["overdue"] == []
        assert s["due_through"] == d("2026-10-21")

    def test_a_series_running_AHEAD_of_the_clock_owes_nothing(self):
        """
        Not reachable from the FTP, but reachable from a hand-loaded row or a
        clock skew, and a negative count here would read as a huge backlog.
        """
        assert overdue("2026-10-28", "2026-10-22T14:15") == []

    def test_the_backlog_is_bounded_rather_than_unbounded(self):
        """
        A decade-old series end must not spin or return thousands of dates
        into a log line.
        """
        s = cme_ftp.publication_status(d("2015-01-02"), dt("2026-10-22T14:15"))
        assert 0 < len(s["overdue"]) <= 400


def test_the_overdue_list_is_oldest_first_and_contiguous():
    """
    The log prints the first few, so the first one named must be the oldest
    missing print -- the one at risk of ageing out of the pull's lookback.
    """
    got = cme_ftp.publication_status(d("2026-10-14"), dt("2026-10-22T16:15"))["overdue"]
    assert got == [d(x) for x in ("2026-10-15", "2026-10-16", "2026-10-19",
                                  "2026-10-20", "2026-10-21")]


@pytest.mark.parametrize("series_end,now,expected", [
    # CME on time, every weekday morning of a normal week.
    ("2026-10-13", "2026-10-15T09:00", []),
    ("2026-10-14", "2026-10-16T09:00", []),
    ("2026-10-15", "2026-10-19T09:00", []),   # Monday, holding Thursday
    # One print missed, each day of the same week.
    ("2026-10-12", "2026-10-15T09:00", ["2026-10-13"]),
    ("2026-10-13", "2026-10-16T09:00", ["2026-10-14"]),
])
def test_on_time_and_one_behind_across_a_week(series_end, now, expected):
    assert overdue(series_end, now) == expected


# ---------------------------------------------------------------------------
# What a scheduled run actually asks CME for.
# ---------------------------------------------------------------------------

REPO = __import__("pathlib").Path(__file__).resolve().parent.parent


def business_days(lo, hi):
    out, d = [], d_(lo)
    while d <= d_(hi):
        if d.weekday() < 5:
            out.append(d)
        d += __import__("datetime").timedelta(days=1)
    return out


def d_(s):
    return date.fromisoformat(s)


def old_window(now, days=4):
    """
    The rule this replaced: every date in a fixed span, held or not. Kept so
    the tests can show what it loses rather than assert the survivor alone.
    """
    import datetime as _dt
    start = now.date() - _dt.timedelta(days=days)
    out, x = [], start
    while x <= now.date():
        out.append(x)
        x += _dt.timedelta(days=1)
    return out


class TestCatchUpPicksTheGap:
    def test_it_asks_only_for_what_is_missing(self):
        have = [x.isoformat() for x in business_days("2026-07-01", "2026-10-19")]
        got = cme_ftp.catch_up_dates(have, dt("2026-10-22T14:15"))
        assert [x.isoformat() for x in got] == ["2026-10-20", "2026-10-21"]

    def test_a_date_already_held_is_not_re_fetched(self):
        """
        The old window re-fetched everything in its span, which is why a
        morning with no arrivals still logged "Ingested 6 days" -- six rows
        rewritten with themselves, and a genuine miss indistinguishable from
        a normal day.
        """
        have = [x.isoformat() for x in business_days("2026-07-01", "2026-10-21")]
        assert cme_ftp.catch_up_dates(have, dt("2026-10-22T14:15")) == []

    def test_weekends_are_never_asked_for(self):
        got = cme_ftp.catch_up_dates([], dt("2026-10-26T14:15"))
        assert got, "nothing was selected at all"
        assert all(x.weekday() < 5 for x in got), (
            "a weekend date was selected: CME publishes no weekend index, so "
            "it is a guaranteed 550 on every run")

    def test_nothing_past_the_due_date_is_asked_for(self):
        """A date CME cannot have published yet is not a gap."""
        got = cme_ftp.catch_up_dates([], dt("2026-10-22T09:00"))
        assert max(got) == d_("2026-10-20"), (
            "asked for a date that is not due yet: %s" % max(got))

    def test_an_interior_hole_is_picked_up_not_just_the_tail(self):
        """
        The 23 prints that were missing on 2026-10-22 were interior holes, not
        a tail gap -- dates CME had published, surrounded on both sides by
        rows we held. A tail-anchored rule would never have asked again.
        """
        have = [x.isoformat() for x in business_days("2026-07-01", "2026-10-21")]
        have.remove("2026-09-15")
        got = cme_ftp.catch_up_dates(have, dt("2026-10-22T14:15"))
        assert d_("2026-09-15") in got


class TestTheOutageIsRecoverable:
    def test_a_three_week_outage_is_still_asked_for_in_full(self):
        """
        THE POINT OF THE CHANGE. CME goes dark for three weeks. Every business
        day in the gap must still be in scope on the day they come back, or
        the oldest are lost for good.
        """
        have = [x.isoformat() for x in business_days("2026-07-01", "2026-09-30")]
        got = cme_ftp.catch_up_dates(have, dt("2026-10-22T14:15"))
        assert got == business_days("2026-10-01", "2026-10-21"), (
            "the outage is not fully in scope: got %s..%s"
            % (min(got), max(got)))

    def test_THE_OLD_WINDOW_WOULD_HAVE_LOST_MOST_OF_IT(self):
        """
        THE GUARD PROVEN AGAINST WHAT IT REPLACED. If this comparison ever
        stops showing a difference, the window has crept back and the test
        above is passing for the wrong reason.
        """
        now = dt("2026-10-22T14:15")
        have = [x.isoformat() for x in business_days("2026-07-01", "2026-09-30")]
        new = set(cme_ftp.catch_up_dates(have, now))
        old = set(old_window(now, days=4))
        lost = sorted(new - old)
        assert len(lost) >= 10, (
            "the four-day window would have lost only %d date(s); this test "
            "is no longer demonstrating anything" % len(lost))
        assert d_("2026-10-01") in lost
        assert min(lost) < d_("2026-10-18")

    def test_the_horizon_bounds_a_run_rather_than_reaching_to_2015(self):
        """
        Fetching only what is missing is what makes a long reach affordable,
        but it is not unbounded: a hole older than the horizon needs a
        deliberate --start, which is how the 2017 one was recovered.
        """
        got = cme_ftp.catch_up_dates([], dt("2026-10-22T14:15"))
        assert min(got) >= d_("2026-10-22") - __import__("datetime").timedelta(
            days=cme_ftp.CATCHUP_HORIZON_DAYS + 1)
        assert len(got) < 70

    def test_the_horizon_survives_an_outage_longer_than_any_on_record(self):
        assert cme_ftp.CATCHUP_HORIZON_DAYS >= 30, (
            "the horizon no longer covers a month-long outage, which is the "
            "condition this was widened for")

    def test_an_empty_table_does_not_reach_back_forever(self):
        got = cme_ftp.catch_up_dates([], dt("2026-10-22T14:15"))
        assert got, "a fresh table asked for nothing at all"
        assert len(got) <= cme_ftp.CATCHUP_HORIZON_DAYS


def test_both_scheduled_jobs_ask_for_the_gap_rather_than_a_window():
    """
    The structural half: a window creeping back into either job is the
    regression, and neither can be unit-tested from Python. Pinned on the
    shipped scripts.
    """
    for name in ("cme_pull.ps1", "daily_update.ps1"):
        p = REPO / "scripts" / name
        if not p.exists():
            pytest.skip("%s not present" % name)
        src = p.read_text(encoding="utf-8")
        call = [ln for ln in src.splitlines()
                if "backfill_ftp.py" in ln and "Start-Process" in ln]
        assert call, "%s no longer invokes backfill_ftp.py" % name
        joined = " ".join(call)
        assert "--catch-up" in joined, (
            "%s is not using --catch-up: %s" % (name, joined))
        assert "--start" not in joined, (
            "%s is back on a fixed lookback window, which is what made a long "
            "outage unrecoverable: %s" % (name, joined))


# ---------------------------------------------------------------------------
# The two gates, and the regression the split exists to prevent.
# ---------------------------------------------------------------------------

import index_dates  # noqa: E402


class TestTheTwoGates:
    def test_the_fetch_gate_is_strictly_earlier_than_the_overdue_gate(self):
        """
        THE INVARIANT THE WHOLE SPLIT RESTS ON. Fetching must start before
        warning does. Reversed, the pull would stop asking for a print at the
        same moment the page started calling it missing -- a dashboard
        complaining about a file nothing is fetching any more.
        """
        early = (index_dates.PUBLISH_EARLIEST_HOUR,
                 index_dates.PUBLISH_EARLIEST_MINUTE)
        late = (index_dates.PUBLISH_OVERDUE_HOUR,
                index_dates.PUBLISH_OVERDUE_MINUTE)
        assert early < late, (
            "PUBLISH_EARLIEST %s is not before PUBLISH_OVERDUE %s" % (early, late))

    def test_the_fetch_gate_opens_at_1304(self):
        before = cme_ftp.fetchable_through(dt("2026-10-22T13:03"))
        after = cme_ftp.fetchable_through(dt("2026-10-22T13:04"))
        assert before == d("2026-10-20")
        assert after == d("2026-10-21")

    def test_an_ordinary_afternoon_is_fetchable_but_not_yet_overdue(self):
        """
        The gap between the gates is the whole point: between 13:04 and 16:05
        the pull asks for today's print and the page says nothing about it.
        """
        now = dt("2026-10-22T14:15")
        assert cme_ftp.fetchable_through(now) == d("2026-10-21")
        assert cme_ftp.due_through(now) == d("2026-10-20")


class TestTheAfternoonPullStillAsks:
    """
    THE REGRESSION THIS SPLIT EXISTS TO PREVENT. catch_up_dates bounds itself
    by fetchable_through. Point it at due_through instead -- the obvious
    "simplification" -- and the 14:15 scheduled pull stops requesting the
    print CME posted at 13:35, every single day, silently.
    """

    def test_the_1415_pull_asks_for_todays_expected_print(self):
        have = [x.isoformat() for x in business_days("2026-07-01", "2026-10-20")]
        got = cme_ftp.catch_up_dates(have, dt("2026-10-22T14:15"))
        assert d_("2026-10-21") in got, (
            "the afternoon pull no longer asks for the print due today: %s" % got)

    def test_that_regression_check_can_actually_fail(self):
        """
        Bound the same call by the OVERDUE gate and 10-21 must drop out --
        proof the test above is asserting something that can be false.
        """
        have = {x.isoformat() for x in business_days("2026-07-01", "2026-10-20")}
        now = dt("2026-10-22T14:15")
        due = cme_ftp.due_through(now)
        would_ask = [x for x in cme_ftp.catch_up_dates(have, now) if x <= due]
        assert d_("2026-10-21") not in would_ask
