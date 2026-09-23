"""
Which barns the index date is still waiting on, and roughly how big they are.

LOG ONLY. Nothing here filters, deletes, gates or withholds anything: the index
is computed exactly as before, the client email goes out exactly as before, and
the only trace of this file in a run is a line or three on stdout. Earlier
attempts here were reverted for touching the last mile to the client. If you
are tempted to make this act on what it finds, read that sentence again.

WHY IT EXISTS. The morning estimate is computed on whatever has been fetched by
then -- usually nearly all of a day, occasionally not, and nothing in the log
said which. When I look at the number in the morning, is a big barn missing?

WHAT IT IS NOT: an alarm. No threshold, no severity word, nothing to calibrate
-- it prints the same two facts every run and a human judges them. Earlier
versions grew tiers, a cents-on-the-index estimator and a holiday calendar;
every one was a knob a mutation could move with the suite still green. A
holiday needs no calendar here: the whole roster reads missing.
"""
from datetime import date, timedelta
from statistics import median

import snowflake_db as db
from bucketing import shifted_bucket_date
from index_dates import headline_index_date

# A barn is EXPECTED on a given weekday if it reported on at least MIN_PRESENT
# of the last OCCURRENCES same-weekday dates. 9 of 12 ages a barn that stops
# selling off the roster on its fourth missed occurrence (12 - k < 9 iff
# k >= 4), and never admits one that sells less often than three weeks in four.
MIN_PRESENT = 9
OCCURRENCES = 12


def barn_days(conn):
    """
    {bucket date: {slug_id: (head, pounds)}} over every stored sale.

    BUCKETED, not reported: shifted_bucket_date() is the day
    recompute_fci_daily() counts a sale on -- El Reno prints Tuesday and
    belongs to Wednesday -- and normalises any backend's date to an ISO key.

    KEYED ON slug_id, never the printed name. Billings MT, La Junta CO and
    Torrington WY are each two reports sharing a city; a name-keyed roster lets
    the half that reported answer for the half that did not.

    Totals ACCUMULATE, because one slug routinely puts several rows in one
    bucket. That is the several weight brackets on the one report, and it is
    the normal case rather than the exception -- 6,223 of the 6,671 stored
    (report_date, location, slug_id) groups carry more than one row (measured
    2026-09-23 over all 32,946 rows).

    TWO REPORT DATES snapping onto one bucket would accumulate the same way,
    and correctly, but it has never happened: on that same measurement not one
    slug has two distinct report_dates landing on a single bucket date. Do not
    take the accumulation out on the strength of that. One slug does already
    reach a bucket twice on one date under two spellings of its location
    (SUPERIOR VIDEO North Central and South Central), and a change to a
    location's bucket shift in bucketing.py could collapse two dates tomorrow.
    """
    days = {}
    for report_date, location, slug_id, head, weight in conn.cursor().execute(
            "SELECT report_date, location, slug_id, head_count, avg_weight "
            "FROM mars_sales").fetchall():
        barns = days.setdefault(shifted_bucket_date(location, report_date), {})
        prev_head, prev_lbs = barns.get(slug_id, (0, 0.0))
        barns[slug_id] = (prev_head + head, prev_lbs + head * weight)
    return days


def expected(days, index_date):
    """
    {slug_id: median pounds} for the barns this weekday normally brings, over
    the OCCURRENCES same-weekday dates STRICTLY BEFORE index_date -- the index
    date is the day being judged, so counting it would let a barn's own absence
    help decide whether it was expected.

    MEDIAN POUNDS, not head: the index is pound-weighted, so pounds are what an
    absence is worth, and head cannot rank a small barn against a large one.
    """
    pounds = {}
    for k in range(1, OCCURRENCES + 1):
        occurrence = (index_date - timedelta(days=7 * k)).isoformat()
        for slug_id, (_head, lbs) in days.get(occurrence, {}).items():
            pounds.setdefault(slug_id, []).append(lbs)
    return {slug_id: median(seen) for slug_id, seen in pounds.items()
            if len(seen) >= MIN_PRESENT}


def _names(conn):
    """{slug_id: "City ST"}. First spelling wins, so the name is stable."""
    names = {}
    for slug_id, location, state in conn.cursor().execute(
            "SELECT DISTINCT slug_id, location, state FROM mars_sales "
            "ORDER BY slug_id, location").fetchall():
        names.setdefault(slug_id, f"{location} {state}")
    return names


def _index_date(conn, days):
    """
    The date to report on: the one the dashboard and the client email lead
    with, so the log names the day they name. See index_dates.py for the rule
    and why it is not MAX(report_date).

    The candidate dates are every bucket date a sale is held for, UNIONED with
    fci_daily's -- not fci_daily's alone, which is what
    notify_email.pending_print_date() uses.

    fci_daily is the half that matters on a day nothing sold: the index still
    has a value there (its 7-day window is not empty) and the email still leads
    with it, so the report must be able to say nothing sold rather than quietly
    back up a day. The bucket dates are the half that survives a backend where
    fci_daily or cme_ftp_daily is unreadable, since both are read inside the
    same try -- a diagnostic should degrade to a staler day, not to no report.

    In the stored history the union adds nothing: every bucket date already has
    an fci_daily row, so the two sets agree and this picks the same day
    notify_email.py does (measured 2026-09-23). It is the fallback that earns
    the union, not a disagreement in normal operation.
    """
    available = {date.fromisoformat(d) for d in days}
    published = None
    cur = conn.cursor()
    try:
        row = cur.execute(
            "SELECT MAX(report_date) FROM cme_ftp_daily").fetchone()
        if row and row[0]:
            published = date.fromisoformat(str(db.iso(row[0])))
        available |= {date.fromisoformat(str(db.iso(r[0]))) for r in
                      cur.execute("SELECT report_date FROM fci_daily")}
    except Exception:                          # noqa: BLE001 -- table absent
        pass
    return headline_index_date(published, available)


def report_lines(conn):
    """
    The report, as a list of strings. CANNOT RAISE, and returns a materialised
    list rather than a generator: this runs after the index is computed and the
    snapshot frozen and before the push, and a diagnostic must never be what
    strands a finished index.
    """
    try:
        days = barn_days(conn)
        index_date = _index_date(conn, days)
        if index_date is None:
            return ["Barn report: no sales stored yet -- nothing to report."]
        roster = expected(days, index_date)
        reported = days.get(index_date.isoformat(), {})
        missing = sorted((s for s in roster if s not in reported),
                         key=lambda s: (-roster[s], s))
        lines = [f"Barn report -- index date {index_date.isoformat()} "
                 f"({index_date:%a}): {len(roster) - len(missing)} of "
                 f"{len(roster)} expected barns reported"]
        if not missing:
            return lines
        names, typical = _names(conn), sum(roster.values())
        rows = [(names[s], f"{roster[s]:,.0f}", roster[s] / typical)
                for s in missing]
        wide = max(len(name) for name, _lbs, _share in rows)
        lbs_wide = max(len(lbs) for _name, lbs, _share in rows)
        for name, lbs, share in rows:
            lines.append(f"  missing: {name:<{wide}}  ~{lbs:>{lbs_wide}} lb  "
                         f"(~{share:.0%} of a typical {index_date:%A})")
        return lines
    except Exception as e:                     # noqa: BLE001 -- deliberate
        return [f"Barn report skipped: {type(e).__name__}: {e}"]
