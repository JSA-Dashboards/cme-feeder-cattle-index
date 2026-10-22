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

The clock is CENTRAL and the band (13:04-14:04) is measured off CME's own MDTM
stamps. scripts/cme_pull.ps1 records the five-hour error that came of reading
those as UTC; nothing here may be re-derived from our ingest logs, which record
when we looked rather than when CME published.
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
    def test_the_2026_10_21_pull_that_called_itself_current(self):
        """
        The live state at 14:15 on 2026-10-21. We held through 10-19; CME's own
        clock says 10-20 was due at 14:05 that afternoon and no FC261020.txt
        ever appeared.
        """
        assert overdue("2026-10-19", "2026-10-21T14:15") == ["2026-10-20"]

    def test_the_2026_10_20_pull_the_day_before_was_genuinely_current(self):
        """
        The control that makes the case above mean something. The SAME job, the
        SAME sentence in the log, one day earlier -- and here it was true.
        """
        assert overdue("2026-10-19", "2026-10-20T14:15") == []

    def test_by_the_next_afternoon_two_prints_are_owed(self):
        assert overdue("2026-10-19", "2026-10-22T14:15") == ["2026-10-20",
                                                             "2026-10-21"]


class TestTheDeadlineWithinTheDay:
    """
    CME publishes index date D on the next business day between 13:04 and
    14:04. Before 14:05 on that day, D is not late -- it is pending.
    """

    def test_before_the_deadline_todays_due_print_is_not_yet_owed(self):
        assert overdue("2026-10-20", "2026-10-22T13:00") == []

    def test_after_the_deadline_it_is(self):
        assert overdue("2026-10-20", "2026-10-22T14:15") == ["2026-10-21"]

    def test_the_boundary_is_1405_exactly(self):
        assert overdue("2026-10-20", "2026-10-22T14:04") == []
        assert overdue("2026-10-20", "2026-10-22T14:05") == ["2026-10-21"]


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
        got = overdue("2026-10-22", "2026-10-26T14:15")
        assert got == ["2026-10-23"], (
            "a weekend date was counted as owed, or Friday's was missed: %s"
            % got)

    def test_a_holiday_is_NOT_skipped(self):
        """
        CME publishes on exchange holidays -- 2026-09-07 was Labor Day and
        their file carries an index value with same-day head 0. A holiday
        calendar here would quietly forgive a genuinely missing print.
        """
        assert overdue("2026-09-04", "2026-09-08T14:15") == ["2026-09-07"]


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
        s = cme_ftp.publication_status(None, dt("2026-10-22T14:15"))
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
    got = cme_ftp.publication_status(d("2026-10-14"), dt("2026-10-22T14:15"))["overdue"]
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
