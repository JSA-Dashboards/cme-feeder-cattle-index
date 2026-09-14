"""
Tests for the headline-date rule.

These exist because the failure they cover is invisible to every other check in
the repo. The forecast scorecard compares our estimate for a date against CME's
print for that same date, so it scores perfectly no matter which date is
headlined -- it measures the number, never the choice of number. On 2026-09-14
the page led with a 480-head Monday window while a complete Friday estimate sat
behind it, and nothing anywhere went red.

The Monday case is the one to keep: it is the only weekday where the rule that
was in place gave the wrong answer, which is exactly why it survived.
"""
from datetime import date

import pytest

from index_dates import headline_index_date, next_index_date


def d(s):
    return date.fromisoformat(s)


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


class TestHeadlineIndexDate:
    def test_the_monday_case_that_caused_this(self):
        """
        The live 2026-09-14 state. CME had published through Friday 9/10;
        fci_daily held rows through Monday 9/14, including weekend
        carry-forwards. The headline must be 9/11 -- complete, and the date CME
        printed that afternoon -- not 9/14, which held one Saturday auction.
        """
        available = {d(x) for x in ("2026-09-10", "2026-09-11", "2026-09-12",
                                    "2026-09-13", "2026-09-14")}
        assert headline_index_date(d("2026-09-10"), available) == d("2026-09-11")

    def test_never_leads_with_a_weekend(self):
        """CME publishes no Saturday or Sunday index; fci_daily still has rows."""
        available = {d(x) for x in ("2026-09-11", "2026-09-12", "2026-09-13")}
        got = headline_index_date(d("2026-09-10"), available)
        assert got.weekday() < 5, f"{got} is a {got.strftime('%A')}"

    def test_midweek_leads_with_the_pending_day(self):
        available = {d(x) for x in ("2026-09-09", "2026-09-10")}
        assert headline_index_date(d("2026-09-09"), available) == d("2026-09-10")

    def test_cme_several_days_behind_leads_with_the_oldest_pending(self):
        """
        What CME prints NEXT, not our newest guess -- the convention CIH dates
        by. A holiday backlog is the normal way this happens.
        """
        available = {d(x) for x in ("2026-09-08", "2026-09-09", "2026-09-10",
                                    "2026-09-11")}
        assert headline_index_date(d("2026-09-08"), available) == d("2026-09-09")

    def test_falls_back_when_cme_is_current(self):
        """No pending print: degrade to the newest row rather than to nothing."""
        available = {d("2026-09-10"), d("2026-09-11")}
        assert headline_index_date(d("2026-09-11"), available) == d("2026-09-11")

    def test_falls_back_when_cme_series_is_missing(self):
        available = {d("2026-09-10"), d("2026-09-11")}
        assert headline_index_date(None, available) == d("2026-09-11")

    def test_no_data_at_all_is_none_not_a_crash(self):
        assert headline_index_date(None, set()) is None
        assert headline_index_date(None, None) is None


@pytest.mark.parametrize("last_published,expected", [
    ("2026-09-07", "2026-09-08"),   # Mon holiday -> Tue
    ("2026-09-08", "2026-09-09"),
    ("2026-09-09", "2026-09-10"),
    ("2026-09-10", "2026-09-11"),
    ("2026-09-11", "2026-09-14"),   # the weekend jump
])
def test_a_full_week(last_published, expected):
    available = {d(f"2026-09-{n:02d}") for n in range(7, 21)}
    assert headline_index_date(d(last_published), available) == d(expected)
