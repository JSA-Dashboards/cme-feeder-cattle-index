"""
Tests for the headline-date rule.

These exist because the failure they cover is invisible to every other check in
the repo. The forecast scorecard compares our estimate for a date against CME's
print for that same date, so it scores perfectly no matter which date is
headlined -- it measures the number, never the choice of number. On 2026-09-14
the page led with a 480-head Monday window while a complete Friday estimate sat
behind it, and nothing anywhere went red.

THE RULE CHANGED ON 2026-10-22, from "the first business day after CME's last
published file" to "the freshest complete estimate". Both rules give the same
answer on any day CME is on time; they differ exactly when CME is late, and the
old one had by then gone wrong twice that way -- once when CME's file layout
changed and froze the headline for three days, once when CME skipped the 10/20
print entirely while CIH and Compass both published 10/21. The tests that
pinned the old behaviour are still here, inverted and labelled, because the
property they protected was real and the reversal was a decision rather than a
bug fix.

Two things are deliberately unchanged and still pinned: the Monday case, and
never leading with a weekend. The new rule reaches both by a different route,
which is the point of keeping them.
"""
from datetime import date

import pytest

import index_dates
from index_dates import central_today, headline_index_date, next_index_date


def d(s):
    return date.fromisoformat(s)


def old_rule(last_published, available):
    """
    The rule as it stood until 2026-10-22, reimplemented here so the tests can
    say which of the two they are choosing rather than merely asserting the
    survivor. Used only to prove the two disagree where they are supposed to.
    """
    if last_published is None:
        return max(available) if available else None
    nxt = next_index_date(last_published)
    if nxt in available:
        return nxt
    return max(available) if available else None


class TestNextIndexDate:
    def test_midweek_is_the_next_day(self):
        assert next_index_date(d("2026-09-09")) == d("2026-09-10")   # Wed -> Thu

    def test_friday_skips_the_weekend(self):
        assert next_index_date(d("2026-09-11")) == d("2026-09-14")   # Fri -> Mon

    def test_saturday_and_sunday_both_land_on_monday(self):
        assert next_index_date(d("2026-09-12")) == d("2026-09-14")
        assert next_index_date(d("2026-09-13")) == d("2026-09-14")

    def test_holidays_are_not_skipped(self):
        # 2026-09-07 was Labor Day and CME published an index for it with
        # same-day head 0. A holiday calendar here would skip a real date.
        assert next_index_date(d("2026-09-04")) == d("2026-09-07")


class TestTheRuleItself:
    def test_the_monday_case_that_caused_this(self):
        """
        The live 2026-09-14 state, and the reason either rule exists. CME had
        published through Friday 9/10; fci_daily held rows through Monday 9/14,
        including weekend carry-forwards. The headline must be 9/11 -- complete,
        and the date CME printed that afternoon -- not 9/14, which held one
        Saturday auction.

        Under the new rule 9/14 is excluded for being TODAY rather than for
        being unprinted, and 9/12 and 9/13 for being a weekend. Same answer,
        different reason, which is why this case is worth keeping.
        """
        available = {d(x) for x in ("2026-09-10", "2026-09-11", "2026-09-12",
                                    "2026-09-13", "2026-09-14")}
        assert headline_index_date(d("2026-09-10"), available,
                                   today=d("2026-09-14")) == d("2026-09-11")

    def test_never_leads_with_a_weekend(self):
        """CME publishes no Saturday or Sunday index; fci_daily still has rows."""
        available = {d(x) for x in ("2026-09-11", "2026-09-12", "2026-09-13")}
        for reader_day in ("2026-09-12", "2026-09-13", "2026-09-14"):
            got = headline_index_date(d("2026-09-10"), available,
                                      today=d(reader_day))
            assert got.weekday() < 5, "%s is a %s" % (got, got.strftime("%A"))

    def test_today_is_never_the_headline_however_late_in_the_day(self):
        """
        A day's own auctions reach AMS the following morning, so today's row is
        the least formed one there is at any hour. The old rule could land on
        it once CME caught up mid-afternoon; this one cannot, because the test
        is the calendar rather than CME's progress.
        """
        available = {d("2026-10-%02d" % n) for n in range(19, 23)}
        assert headline_index_date(d("2026-10-21"), available,
                                   today=d("2026-10-22")) == d("2026-10-21")

    def test_yesterday_is_the_headline_on_an_ordinary_morning(self):
        available = {d(x) for x in ("2026-09-09", "2026-09-10")}
        assert headline_index_date(d("2026-09-09"), available,
                                   today=d("2026-09-11")) == d("2026-09-10")


class TestWhenCmeIsLate:
    """
    The reversal. Each of these asserts the OPPOSITE of what the suite asserted
    before 2026-10-22, and each names the morning that changed it.
    """

    def test_the_2026_10_22_morning(self):
        """
        CME published no FC261020.txt at all. Our 10/21 estimate was finished
        and correct -- $343.5726, which CIH and Compass both published to the
        cent that morning -- and the old rule led with 10/20 because that was
        the print CME owed next.
        """
        available = {d("2026-10-%02d" % n) for n in range(14, 22)}
        got = headline_index_date(d("2026-10-19"), available,
                                  today=d("2026-10-22"))
        assert got == d("2026-10-21")
        assert old_rule(d("2026-10-19"), available) == d("2026-10-20"), \
            "the old rule is being reimplemented wrongly -- this test proves nothing"
        assert got != old_rule(d("2026-10-19"), available)

    def test_the_2026_09_14_layout_change_no_longer_freezes_the_headline(self):
        """
        CME changed their file layout, cme_ftp.py skipped every row and
        cme_ftp_daily stopped at 09/11. The page headlined 09/14 for three days
        while our estimates ran on to 09/16, and the daily job exited 0
        throughout because the pipeline was fine -- only the CME feed had
        stopped. The headline now tracks our own data instead.
        """
        available = {d("2026-09-%02d" % n) for n in range(8, 17)}
        got = headline_index_date(d("2026-09-11"), available,
                                  today=d("2026-09-17"))
        assert got == d("2026-09-16")
        assert old_rule(d("2026-09-11"), available) == d("2026-09-14")

    def test_a_long_cme_outage_does_not_drag_the_headline_backwards(self):
        """
        The property that matters most: however far behind CME falls, the
        headline stays on our freshest complete date. Under the old rule the
        headline moved FURTHER from today the longer CME was out.
        """
        available = {d("2026-10-%02d" % n) for n in range(5, 22)}
        for stuck_at in ("2026-10-19", "2026-10-15", "2026-10-09", "2026-10-05"):
            assert headline_index_date(d(stuck_at), available,
                                       today=d("2026-10-22")) == d("2026-10-21")

    def test_it_agrees_with_the_old_rule_whenever_cme_is_on_time(self):
        """
        The reversal is meant to be invisible on a normal day. CME publishes
        index date D on D+1, so on the morning of D+1 its last file is D-1 and
        both rules say D.
        """
        available = {d("2026-10-%02d" % n) for n in range(5, 23)}
        for today_, last_pub in (("2026-10-14", "2026-10-12"),
                                 ("2026-10-15", "2026-10-13"),
                                 ("2026-10-16", "2026-10-14"),
                                 ("2026-10-20", "2026-10-16")):
            new = headline_index_date(d(last_pub), available, today=d(today_))
            assert new == old_rule(d(last_pub), available), \
                "they disagree on an on-time day: %s" % today_


class TestFallbacks:
    def test_cme_current_still_leads_with_the_freshest_complete_date(self):
        available = {d("2026-09-10"), d("2026-09-11")}
        assert headline_index_date(d("2026-09-11"), available,
                                   today=d("2026-09-14")) == d("2026-09-11")

    def test_a_missing_cme_series_degrades_rather_than_blanking(self):
        available = {d("2026-09-10"), d("2026-09-11")}
        assert headline_index_date(None, available,
                                   today=d("2026-09-14")) == d("2026-09-11")

    def test_no_data_at_all_is_none_not_a_crash(self):
        assert headline_index_date(None, set(), today=d("2026-09-14")) is None
        assert headline_index_date(None, None, today=d("2026-09-14")) is None

    def test_an_unknown_today_takes_the_OLD_rule_not_a_guess(self):
        """
        today=None means the zone could not be resolved. Guessing with the
        server's clock is what would put a UTC host on tomorrow's date; the old
        rule lags a late CME but never invents a date.
        """
        available = {d("2026-10-%02d" % n) for n in range(14, 22)}
        assert headline_index_date(d("2026-10-19"), available, today=None) \
            == old_rule(d("2026-10-19"), available)

    def test_nothing_complete_yet_falls_through_rather_than_returning_none(self):
        """Every row we hold is today or later -- take the old rule's answer."""
        available = {d("2026-10-22"), d("2026-10-23")}
        got = headline_index_date(d("2026-10-21"), available,
                                  today=d("2026-10-22"))
        assert got is not None


class TestTodayIsDerivedAndIsCentral:
    def test_today_is_derived_from_central_today_when_not_passed(self, monkeypatch):
        """
        THE WIRING, AND IT IS LOAD-BEARING. If the default stopped reaching
        central_today(), every caller would silently keep the old behaviour --
        barn_report, notify_email, the letter and both dashboards all rely on
        the default rather than passing a date.
        """
        available = {d("2026-10-%02d" % n) for n in range(14, 22)}
        monkeypatch.setattr(index_dates, "central_today", lambda: d("2026-10-22"))
        assert headline_index_date(d("2026-10-19"), available) == d("2026-10-21")
        monkeypatch.setattr(index_dates, "central_today", lambda: d("2026-10-21"))
        assert headline_index_date(d("2026-10-19"), available) == d("2026-10-20")

    def test_an_explicit_None_is_not_the_same_as_leaving_it_alone(self, monkeypatch):
        """
        The sentinel earns its keep here. A plain None default would make
        "unknown" and "work it out" the same call, and the fallback would never
        run on the machine that needs it.
        """
        available = {d("2026-10-%02d" % n) for n in range(14, 22)}
        monkeypatch.setattr(index_dates, "central_today", lambda: d("2026-10-22"))
        assert headline_index_date(d("2026-10-19"), available) == d("2026-10-21")
        assert headline_index_date(d("2026-10-19"), available, today=None) \
            == d("2026-10-20")

    def test_central_today_is_a_date_or_none_and_never_raises(self):
        got = central_today()
        assert got is None or isinstance(got, date)

    def test_central_today_is_central_not_the_process_timezone(self, monkeypatch):
        """
        The dashboard runs on Streamlit Community Cloud in UTC, where
        date.today() rolls over at 19:00 Central. Reading the server's date
        would move the headline onto a date whose own auctions have not been
        reported yet, every evening. Pinned by construction: central_today()
        must agree with America/Chicago, whatever TZ the process is in.
        """
        zoneinfo = pytest.importorskip("zoneinfo")
        from datetime import datetime
        expected = datetime.now(zoneinfo.ZoneInfo("America/Chicago")).date()
        assert central_today() == expected


@pytest.mark.parametrize("today_,expected", [
    ("2026-09-08", "2026-09-07"),   # Tue -> Mon holiday, which CME does print
    ("2026-09-09", "2026-09-08"),
    ("2026-09-10", "2026-09-09"),
    ("2026-09-11", "2026-09-10"),
    ("2026-09-14", "2026-09-11"),   # Mon -> Fri, skipping the weekend
    ("2026-09-15", "2026-09-14"),
])
def test_a_full_week_of_readers(today_, expected):
    """
    Walked by the day the page is READ rather than by CME's last file, which is
    the whole shape of the change. CME is held deliberately stale at 09/04 so
    every answer here comes from the new rule alone.
    """
    available = {d("2026-09-%02d" % n) for n in range(4, 21)}
    assert headline_index_date(d("2026-09-04"), available,
                               today=d(today_)) == d(expected)


def test_the_headline_no_longer_encodes_cmes_lag_and_that_is_the_trade():
    """
    What was given up, stated so it is not rediscovered as a bug. The old rule
    made (newest row - headline) a measure of how far behind CME was; a caller
    wanting that now subtracts CME's last published date, which is what the
    Last CME Print tile already shows.
    """
    available = {d("2026-09-%02d" % n) for n in range(8, 17)}
    head = headline_index_date(d("2026-09-11"), available, today=d("2026-09-17"))
    assert (max(available) - head).days == 0, "the headline now tracks our data"
    assert (head - d("2026-09-11")).days == 5, "CME's lag is still measurable"
