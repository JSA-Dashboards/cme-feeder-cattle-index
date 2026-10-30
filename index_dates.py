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

THE RULE IS THE FRESHEST COMPLETE ESTIMATE: the most recent date CME can print
that is far enough along to stand behind. Completeness is measured against
TODAY rather than against CME, because what makes a date complete is the AMS
reporting lag, which has nothing to do with when CME gets round to publishing.

WHAT THIS REPLACED, AND WHY. Until 2026-10-22 the rule was "the first business
day after CME's last published file" -- CME's own publication clock. It gives
the same answer as this one on any day CME is on time, and it fails whenever
CME is not:

  * 2026-09-14  CME changed their file layout, cme_ftp.py skipped every row and
                cme_ftp_daily stopped at 09/11. The page headlined 09/14 for
                three days while our own estimates ran on to 09/16. The daily
                job exited 0 throughout and the freshness line stayed green,
                because the pipeline was fine -- only the CME feed had stopped.
  * 2026-10-22  CME simply did not publish the 10/20 print; there was no
                FC261020.txt on their FTP at all. Our 10/21 estimate was
                finished and correct -- $343.5726, which both CIH and Compass
                published to the cent that morning -- and the page led with
                10/20, because that was the print CME owed next.

Following CME's publication clock means going dark exactly when CME does, on a
number clients compare against two competing desks every morning. The old rule
was chosen partly because it was "the convention CIH dates its daily sheet by";
that premise turned out to be false -- on 2026-10-21 CIH's sheet was dated
10/21 while the print CME owed next was 10/20.

WHAT WAS GIVEN UP. The old rule guaranteed the headline was always a date CME
had NOT yet printed, so the tile always read "JSA FCI Estimate". Under this
rule, once CME catches up the headline may be a date CME has published; app.py
already relabels that tile "Current Index" off the row's own source, so it
stays honest. The headline also no longer encodes how far behind CME is -- the
Last CME Print tile and the Pending CME Prints section still say so, and say it
in dates rather than by implication.

Note this is a DISPLAY rule. It changes nothing about how the index is
computed; it only decides which already-computed date is the headline.

The scorecard cannot catch a mistake here: it compares our estimate for a date
against CME's print for that same date, so every date stays individually
accurate no matter which one is headlined. That is why this has tests.
"""
from datetime import date, datetime, timedelta

# Distinguishes "work today out for yourself" from an explicit today=None,
# which means "today is genuinely unknown -- take the fallback". A plain None
# default could not tell those apart, and the difference decides whether a
# caller that cannot resolve a timezone gets the careful rule or a loose one.
_DERIVE = object()


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


def central_today():
    """
    Today's date in Central, or None if the IANA database is unavailable.

    CENTRAL, NOT THE SERVER'S CLOCK, and the difference is not academic: the
    dashboard runs on Streamlit Community Cloud in UTC, where date.today()
    rolls over at 19:00 Central. Taking the server's date would move the
    headline onto a date whose own auctions have not been reported yet, every
    evening -- the 2026-09-14 failure in a different costume, and arriving
    daily rather than once.

    Returns None rather than guessing when the zone cannot be resolved, which
    the caller turns into the conservative fallback rather than a wrong answer.
    """
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Chicago")).date()
    except Exception:
        return None


def headline_index_date(last_published, available=None, today=_DERIVE):
    """
    The index date to lead with.

    last_published  the most recent date CME has actually published, or None
    available       dates we hold a value for, or None to skip the check
    today           today's date in Central. Left alone it is worked out from
                    the clock; pass a date to pin it, or None to say it cannot
                    be determined and take the fallback.

    A date qualifies when it is a weekday and strictly earlier than today.

    STRICTLY EARLIER, because a day's own auctions reach AMS the FOLLOWING
    morning -- about 85% of qualifying head by 07:30 and 95.8% by noon -- so
    today's row is always the least formed one there is, whatever the hour.
    On 2026-09-14 the newest row held 480 head from one Saturday auction; this
    rule headlines 9/11 that Monday, the same answer the old one reached by a
    different route, which is why the Monday case still belongs in the tests.

    WEEKDAY, because CME publishes no Saturday or Sunday index while fci_daily
    carries Friday's value onto them so the series has no holes. Exchange
    holidays are deliberately NOT excluded: CME does publish on them, so a
    holiday calendar would skip a date CME does not.

    FALLBACK, when today is unknown or nothing qualifies: the previous rule,
    the first business day after CME's last published file. It lags a late CME
    but it never invents a date, which is the right way to fail -- and it keeps
    a machine with no tzdata off a headline it cannot justify.
    """
    if today is _DERIVE:
        today = central_today()

    if today is not None and available:
        ready = [d for d in available if d.weekday() < 5 and d < today]
        if ready:
            return max(ready)

    if last_published is None:
        return max(available) if available else None
    nxt = next_index_date(last_published)
    if available is None:
        return nxt
    if nxt in available:
        return nxt
    # CME is current with everything we hold -- nothing pending to lead with.
    return max(available) if available else None


# CME's own publication clock, in one place so a guard can ask it a question
# rather than assume an answer. Both gates are measured off their MDTM stamps
# and are CENTRAL -- see the header of scripts/cme_pull.ps1 for the five-hour
# error that reading them as UTC caused, and for why nothing here may be
# re-derived from our own ingest logs, which record when we looked rather than
# when CME published.
#
# TWO GATES, NOT ONE, AND THEY WANT OPPOSITE VALUES. There used to be a single
# PUBLISH_DEADLINE of 14:05 serving both jobs below, and that is how its value
# drifted without anyone noticing: moving it to fix either job silently broke
# the other, so it sat at a number that was wrong for both.
#
#   EARLIEST -- "could be on the server by now". Gates what
#   cme_ftp.catch_up_dates() will ATTEMPT to fetch, because a date past it is
#   never asked for at all. Wants to be EARLY: set it late and the scheduled
#   afternoon pull stops requesting today's print and the file waits for the
#   next morning.
#
#   OVERDUE -- "should be on the server by now". Gates publication_status(),
#   and through it the client-visible "CME is N prints behind" banner on the
#   dashboard. Wants to be LATE: set it early and the page accuses CME of
#   lateness on an ordinary afternoon.
#
# MEASURED 2026-10-30 off 159 business-day index dates' MDTM stamps:
#
#     min 08:05   p10 13:04   median 13:35   p90 15:05   p99 16:04   max 20:48
#
# so the band is 13:04-15:05 for ordinary prints. The 13:04-14:04 this file
# carried until then captured only 50.3% of prints and called 42.1% of them
# late. 15:05 is also exactly what the 2026-10-07 five-hour correction should
# have produced -- the pre-correction worst case was 10:05, and 10:05+5h is
# 15:05 -- but the correction carried the old MEDIAN (09:04 -> 14:04) across as
# the ceiling instead of the old worst case.
#
# OVERDUE IS 16:05 RATHER THAN 15:05 because an MDTM stamp is when CME WROTE
# the file, not when it can be READ. Measured 2026-10-28: stamped 14:34:46,
# first successfully fetched 14:51:49, with the path 550-ing as though absent
# in between. One observation, so the hour of headroom is deliberate slack
# rather than a computed figure. At 16:05 the banner would have fired wrongly
# on 1.3% of those 159 days; at the old 14:05 it would have fired on 28.3%,
# or 48.4% once that propagation gap is allowed for.
PUBLISH_EARLIEST_HOUR = 13
PUBLISH_EARLIEST_MINUTE = 4
PUBLISH_OVERDUE_HOUR = 16
PUBLISH_OVERDUE_MINUTE = 5


def _through(now, hour, minute):
    """The newest index date whose publication day has reached (hour, minute).

    Weekdays only, matching next_index_date: CME publishes on exchange
    holidays (2026-09-07, Labor Day, carries a value with same-day head 0), so
    a holiday calendar would call a real date unpublishable.
    """
    d = now.date()
    for _ in range(14):               # bounded; a fortnight of weekends
        d -= timedelta(days=1)
        if d.weekday() >= 5:
            continue
        pub_day = next_index_date(d)
        if pub_day < now.date():
            return d
        if pub_day == now.date() and (now.hour, now.minute) >= (hour, minute):
            return d
    return None


def fetchable_through(now):
    """The newest index date that COULD be on the server by `now` (Central).

    What to bother asking CME for. Uses the EARLIEST gate, so the afternoon
    pull requests today's print as soon as one could plausibly exist rather
    than waiting for the hour at which its absence would be news.
    """
    return _through(now, PUBLISH_EARLIEST_HOUR, PUBLISH_EARLIEST_MINUTE)


def due_through(now):
    """The newest index date that SHOULD be on the server by `now` (Central).

    What CME owes. Uses the OVERDUE gate, so a date is only called late once
    an ordinary print would have landed -- including the time it takes to
    become readable after CME writes it.
    """
    return _through(now, PUBLISH_OVERDUE_HOUR, PUBLISH_OVERDUE_MINUTE)


def publication_status(series_end, now):
    """
    What CME still owes us: {series_end, due_through, overdue}.

    `overdue` is every index date after the newest one we hold, up to and
    including the newest one due -- oldest first, empty when CME is current.

    THIS EXISTS BECAUSE ABSENCE WAS UNCONDITIONALLY NORMAL. backfill_ftp.py
    counts a 550 from the server as "no file (weekend/holiday/not yet
    published)" and exits 0, which is right for a Saturday and wrong for a
    print that was due yesterday and never came. On 2026-10-21 the 14:15 pull
    caught nothing and logged the same sentence it had logged on 2026-10-20
    when it caught a real print -- one line, two opposite outcomes, no way to
    tell them apart. Asking this function turns that into a statement that can
    be false.
    """
    due = due_through(now)
    out = {"series_end": series_end, "due_through": due, "overdue": []}
    if due is None or series_end is None:
        return out
    d = series_end
    while len(out["overdue"]) < 400:   # a decade of gap; stop rather than spin
        d = next_index_date(d)
        if d > due:
            break
        out["overdue"].append(d)
    return out
