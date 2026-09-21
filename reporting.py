"""
How complete is the input behind an index date?

The obvious metric -- this date's head against a historical average -- would
lie, and measurably so. Window head swings 34-53% of its median from week to
week (measured 2026-09-10 over five weeks), because that is real variation in
how many cattle actually sell. A light sale week would read as "60% reporting"
while every barn had in fact reported, and a gauge that cries wolf gets ignored.

So this counts BARNS, not volume. That works because sale barns keep to a
weekday: of 60 locations with four or more sales since 2026-06-01, 55 (92%)
sell on the same weekday at least 90% of the time -- Carthage always Monday,
Beaver always Tuesday, El Reno Tuesday 93 of 100. "Which barns were expected
today" is therefore a well-defined set rather than a guess.

Head still appears, but only as each MISSING barn's own typical contribution,
which is the honest way to say whether an absence matters: one small barn
missing is noise, Beaver missing is not.
"""
import re
from collections import Counter, defaultdict
from datetime import date, timedelta
from statistics import median

import snowflake_db as db
from bucketing import shifted_bucket_date

# A barn counts as "expected" on a weekday if it sold on at least this many of
# the recent occurrences of that weekday. 3 of the last 8 keeps genuinely
# regular sellers while dropping one-off consignments and barns that have gone
# quiet -- a barn that stopped selling in July should not be reported missing
# every week thereafter.
MIN_APPEARANCES = 3
RECENT_WEEKS = 8

# Ignore barns whose typical contribution is below this. Their presence or
# absence cannot move a 10,000-head window enough to matter, and listing them
# buries the ones that can.
MIN_TYPICAL_HEAD = 25


def _norm(s):
    return re.sub(r"\s+", " ", (s or "").strip())


def _history(conn, since_iso):
    """{location: {weekday: [(bucket_date, head), ...]}} over the lookback."""
    rows = conn.cursor().execute(
        f"SELECT location, report_date, SUM(head_count) FROM mars_sales "
        f"WHERE report_date >= {db.placeholders(1)} GROUP BY location, report_date",
        (since_iso,),
    ).fetchall()
    hist = defaultdict(lambda: defaultdict(list))
    for loc, rd, head in rows:
        iso = str(db.iso(rd))
        d = date.fromisoformat(iso)
        hist[_norm(loc)][d.weekday()].append((iso, int(head or 0)))
    return hist


def completeness(conn, index_date_iso, weeks=RECENT_WEEKS):
    """
    Reporting completeness for the newest day of an index date's window.

    Returns a dict with:
        expected / present / missing   barn counts
        pct                            present / expected, or None when nothing
                                       is expected (weekends)
        missing_barns                  [(name, typical_head)] worst first
        missing_head                   summed typical head of the absentees
        note                           a plain-English caveat, or ""

    Judges the index date's OWN day only. Earlier days in the 7-day window were
    settled by previous runs, so re-checking them every morning would report the
    same long-dead absences forever and dilute the signal from today.
    """
    target = date.fromisoformat(index_date_iso)
    since = (target - timedelta(weeks=weeks)).isoformat()
    hist = _history(conn, since)
    wd = target.weekday()

    # Which barns are regulars on this weekday, and what do they usually bring?
    expected = {}
    for loc, byday in hist.items():
        sales = byday.get(wd, [])
        # Exclude the target date itself from its own baseline.
        prior = [(iso, h) for iso, h in sales if iso < index_date_iso]
        if len(prior) < MIN_APPEARANCES:
            continue
        typical = median(h for _, h in prior)
        if typical < MIN_TYPICAL_HEAD:
            continue
        expected[loc] = typical

    if not expected:
        return {"expected": 0, "present": 0, "missing": 0, "pct": None,
                "missing_barns": [], "missing_head": 0,
                "note": "no barns sell on this weekday, so nothing is expected"}

    present = {loc for loc, byday in hist.items()
               if any(iso == index_date_iso for iso, _ in byday.get(wd, []))}

    missing = {loc: h for loc, h in expected.items() if loc not in present}
    n_present = len(expected) - len(missing)
    return {
        "expected": len(expected),
        "present": n_present,
        "missing": len(missing),
        "pct": 100.0 * n_present / len(expected),
        "missing_barns": sorted(missing.items(), key=lambda kv: -kv[1]),
        "missing_head": int(sum(missing.values())),
        "note": "",
    }


def summary_line(c):
    """One line for a caption or an email, or None when nothing is expected."""
    if c["pct"] is None:
        return None
    s = (f"{c['present']} of {c['expected']} expected barns reported "
         f"({c['pct']:.0f}%)")
    if c["missing"]:
        worst = ", ".join(f"{n} (~{int(h):,})" for n, h in c["missing_barns"][:3])
        more = f" +{c['missing'] - 3} more" if c["missing"] > 3 else ""
        s += f" — missing ~{c['missing_head']:,} typical head: {worst}{more}"
    return s


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv
    load_dotenv()
    conn = db.get_conn()
    dates = sys.argv[1:] or [str(db.iso(conn.cursor().execute(
        "SELECT MAX(report_date) FROM fci_daily").fetchone()[0]))]
    for d in dates:
        c = completeness(conn, d)
        wd = date.fromisoformat(d).strftime("%A")
        print(f"\n{d} ({wd}):")
        if c["pct"] is None:
            print(f"   {c['note']}")
            continue
        print(f"   {summary_line(c)}")
        for n, h in c["missing_barns"]:
            print(f"      missing: {n:<32} typically {int(h):,} head")
    conn.close()
