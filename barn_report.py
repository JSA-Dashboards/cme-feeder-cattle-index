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

# The two reasons a roster barn contributes nothing, which call for opposite
# responses: one day is incomplete, the other is whole. Both still print, so a
# barn whose qualifying cattle quietly stopped parsing cannot hide behind the
# benign wording. NO_REPORT keeps the original spelling so the line a reader
# already knows is unchanged when nothing else is.
NO_REPORT = "missing:"
NOTHING_QUALIFYING = "no qualifying cattle:"


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


def typical_day(days, index_date):
    """
    Median TOTAL pounds this weekday normally brings -- EVERY barn that sold,
    not just the roster -- over the same occurrences expected() reads.

    THE DENOMINATOR USED TO BE sum(roster.values()), which is not what the line
    claims and is not close to it. The roster holds only barns that sell nearly
    every week; a Friday's volume is mostly direct-trade reports that do not
    report often enough to qualify. On 2026-10-02 the Friday roster was 43,018
    lb against a real Friday of 1.53M, so Belen NM printed as "~19% of a
    typical Friday" when it was 0.5% -- a 36x overstatement, on the one line
    whose job is to say whether a number is safe to send to clients.

    The suite could not catch it. Every fixture had the whole roster reporting
    every week and nothing outside it, so sum(roster) and the day's own total
    were the same number and the two denominators were indistinguishable.
    test_share_is_against_the_whole_day_not_just_the_roster now puts a sporadic
    barn in the data, which is what tells them apart.

    Returns None when no occurrence has sales; the caller then prints no share
    at all. A missing percentage is honest, a wrong one is what this fixes.
    """
    totals = []
    for k in range(1, OCCURRENCES + 1):
        occurrence = (index_date - timedelta(days=7 * k)).isoformat()
        barns = days.get(occurrence)
        if barns:
            totals.append(sum(lbs for _head, lbs in barns.values()))
    return median(totals) if totals else None


def reported_without_qualifying(conn, index_date):
    """
    slug_ids that filed a report for this bucket date and put nothing in the
    index -- the barn sold, but none of it was 700-899 lb Medium & Large #1 or
    #1-2 steers.

    "Missing" covered both this and a barn that never reported, and they mean
    opposite things: this day is COMPLETE. Belen NM on 2026-10-02 filed 19 lots
    of Medium & Large #1 and #1-2 steers, every one of them under 700 lb, and
    the report called it out as missing on an index that was whole.

    READS calf_sales, which is ingested from the SAME AMS barn reports through a
    wider 400-900 lb band -- so a barn present there and absent from mars_sales
    filed and did not qualify. This module is LOG ONLY (see the file docstring):
    nothing here can reach the index, which is why the cash series is safe to
    read from it and is not safe to read from the modules
    tests/test_index_isolation.py guards.

    calf_sales is OPTIONAL and written AFTER the index push (CLAUDE.md, step
    order), so on a same-morning report it can lag a cycle and a barn reads as
    no-report briefly. That is the old wording, which is the safe direction.

    Empty set if the table is absent or unreadable -- every caller then gets
    exactly the behaviour that predated this function.
    """
    want = index_date.isoformat()
    lo = (index_date - timedelta(days=7)).isoformat()
    hi = (index_date + timedelta(days=7)).isoformat()
    seen = set()
    # db.placeholders(), NOT a literal "?". SQLite takes ? and Snowflake takes
    # %s, and the first version of this hardcoded ? -- so on the DEPLOYED
    # backend every call raised "not all arguments converted during string
    # formatting", the except below swallowed it, and every barn read "missing"
    # exactly as it had before this function existed. It shipped that way and
    # Ross spotted it on the live page.
    #
    # The tests could not catch it: every fixture is sqlite3, where ? is right.
    # tests/test_barn_report.py::test_no_sql_here_hardcodes_a_placeholder is the
    # guard that can, because it reads the source rather than running it.
    ph = db.placeholders(2).split(",")
    try:
        rows = conn.cursor().execute(
            "SELECT report_date, location, slug_id FROM calf_sales "
            "WHERE report_date BETWEEN {} AND {}".format(ph[0].strip(), ph[1].strip()),
            (lo, hi)).fetchall()
    except Exception as e:                     # noqa: BLE001 -- table absent
        # SAY SO. Returning an empty set silently is what turned a TypeError
        # into four hours of the page quietly printing the old wording.
        print("  [warn] barn report could not read calf_sales: {}: {}".format(
            type(e).__name__, e))
        return set()
    for report_date, location, slug_id in rows:
        if shifted_bucket_date(location, report_date) == want:
            seen.add(slug_id)
    return seen


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
        # THE REAL COUNTS FIRST, the roster ratio second.
        #
        # "1 of 2 expected barns reported" was true of the roster and useless as
        # a description of 2026-10-02, when 11 barns put cattle in the index and
        # 13 filed. The roster admits only barns selling 9 of 12 same-weekday
        # dates, and measured across the last 8 occurrences of each weekday it
        # covers a minority everywhere -- Mon 6 of 8, Tue 6 of 8, Wed 4 of 10,
        # Thu 8 of 14, Fri 2 of 9.
        #
        # NO THRESHOLD FIXES THAT, which is why the ratio is kept rather than
        # retuned. Loosening MIN_PRESENT to 4 takes Friday to 6 against 9 -- still
        # short -- while Thursday's roster reaches 16 against 14 barns that exist.
        # Barn attendance is genuinely irregular and differs by weekday, so
        # "how many barns should report today" has no stable answer.
        #
        # A POUNDS COVERAGE FIGURE WAS TRIED AND IS WORSE, for now: against a
        # 12-occurrence median it ranges 99%-733% over the last three weeks, and
        # even a 4-occurrence median spreads 77%-258%. The ingest has grown too
        # fast for any backward-looking norm -- direct trade only joined on
        # 2026-08-28 -- so today always looks enormous against its own history. A
        # 12-week baseline fully inside the current regime arrives around
        # 2026-11-20; revisit then, and measure before trusting it.
        #
        # What the roster IS good at is the list below: a barn that sells nearly
        # every week and did not is real signal. The ratio stays as the headline
        # for that list, and the counts in front of it say what the day was.
        #
        # NO RATIO. "1 of 2 expected barns reported" beside "11 barns in the
        # index" reads as a contradiction -- if eleven reported, why do we
        # expect two? -- and the honest answer is that 2 is the ROSTER size, an
        # internal detail that happens to be tiny on a Friday. It is also
        # redundant: the list underneath already names every regular that is
        # out, with its pounds. A denominator nobody can interpret is worse
        # than no denominator.
        #
        # The count of barns that usually sell is still here, in words, because
        # that is the fact the reader needs: is anybody who normally shows up
        # missing. app.py::_barn_header_is_healthy() matches this shape by
        # regex now rather than by counting tokens; both copies changed with it.
        filed = reported_without_qualifying(conn, index_date)
        in_index = len(reported)
        total_filed = in_index + len({s for s in filed if s not in reported})
        day = f"{index_date:%A}"
        if missing:
            tail = (f"{len(missing)} barn{'s' if len(missing) != 1 else ''} that "
                    f"usually sell{'' if len(missing) != 1 else 's'} on a {day} "
                    f"{'have' if len(missing) != 1 else 'has'} not:")
        else:
            tail = f"every barn that usually sells on a {day} is in"
        lines = [f"Barn report -- index date {index_date.isoformat()} "
                 f"({index_date:%a}): {in_index} barns in the index"
                 + (f" ({total_filed} filed)" if total_filed != in_index else "")
                 + f" — {tail}"]
        if not missing:
            return lines
        names = _names(conn)
        typical = typical_day(days, index_date)
        rows = [(NOTHING_QUALIFYING if s in filed else NO_REPORT,
                 names[s], f"{roster[s]:,.0f}",
                 None if not typical else roster[s] / typical)
                for s in missing]
        # Labels pad to the widest in play, so an all-NO_REPORT day renders
        # byte-for-byte as it did before this split existed.
        lab_wide = max(len(lab) for lab, _n, _lbs, _share in rows)
        wide = max(len(name) for _lab, name, _lbs, _share in rows)
        lbs_wide = max(len(lbs) for _lab, _name, lbs, _share in rows)
        for lab, name, lbs, share in rows:
            tail = ("" if share is None else
                    f"  (~{share:.0%} of a typical {index_date:%A})")
            lines.append(f"  {lab:<{lab_wide}} {name:<{wide}}  "
                         f"~{lbs:>{lbs_wide}} lb{tail}")
        return lines
    except Exception as e:                     # noqa: BLE001 -- deliberate
        return [f"Barn report skipped: {type(e).__name__}: {e}"]
