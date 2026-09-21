"""
Which index date a display should lead with.

Extracted into one pure function because getting it wrong is silent. On
2026-09-14 both the dashboard and the daily email led with MAX(report_date)
from fci_daily, and headlined a 9/14 window holding 480 head from a single
Saturday auction while the complete 9/11 estimate -- the one CME printed that
afternoon, and the one CIH dated its sheet by -- sat behind it.

WHY "NEWEST ROW" IS THE WRONG RULE. It is a proxy for "the number that
matters", and it holds Tuesday through Friday: by 07:30 about 85% of the
previous day's qualifying head is already fetchable. It breaks on Monday,
because the newest row advances three calendar days while only Saturday's sale
arrives -- Monday's own auctions, El Reno among them, do not publish until
Tuesday, and a Monday index date normally carries about 3,300 head of its own.
It breaks again at weekends, where CME publishes no index at all but fci_daily
carries Friday's value forward so the series has no holes.

THE RULE USED INSTEAD is the first business day after CME's last published
file. That is CME's own publication clock rather than the calendar, so a
holiday or a late file moves it with no special case, and it is the convention
CME names its files by and CIH dates its daily sheet by.

Note this is a DISPLAY rule. It changes nothing about how the index is
computed; it only decides which already-computed date is the headline. The
forward estimates past it are still shown, in the Pending CME Prints section.

The scorecard cannot catch a mistake here: it compares our estimate for a date
against CME's print for that same date, so every date stays individually
accurate no matter which one is headlined. That is why this has tests.
"""
from datetime import date, timedelta


def next_index_date(after: date) -> date:
    """
    The next date CME can publish an index for, strictly after `after`.

    Weekends only. CME does publish on exchange holidays -- 2026-09-07 was
    Labor Day and their file carries an index value with same-day head 0 -- so
    a holiday calendar would skip a date CME does not.
    """
    d = after + timedelta(days=1)
    while d.weekday() >= 5:          # 5 Sat, 6 Sun
        d += timedelta(days=1)
    return d


def headline_index_date(last_published, available=None):
    """
    The index date to lead with.

    last_published  the most recent date CME has actually published, or None
    available       dates we hold a value for, or None to skip the check

    Falls back to the newest available date when CME's series is missing or
    has run ahead of ours, so a failure here degrades to the old behaviour
    rather than to a blank page.
    """
    if last_published is None:
        return max(available) if available else None
    nxt = next_index_date(last_published)
    if available is None:
        return nxt
    if nxt in available:
        return nxt
    # CME is current with everything we hold -- nothing pending to lead with.
    return max(available) if available else None
