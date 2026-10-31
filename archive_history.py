"""
CME's restated archive, arranged for the page. IT IS NOT THE SCORECARD.

`cme_ftp_daily.fci_value` is the REPORTED INDEX CME printed on the day -- the
independent number this project's estimate is graded against, and its whole
value is that nobody here computed it. `cme_archive_daily.seven_day_wtd_price`
is a weighted average CME recomputed later and published in its FC<year>.ZIP
workbooks. The two are different quantities and they have drifted apart in the
restating, by up to 2.3374 in 2018.

So THIS MODULE NEVER MERGES THEM. It returns the archive series on its own and
the agreement between the two as a SEPARATE table, and there is no function
here that will hand back one series containing both. A page that wants to say
"the index was X on this date in 2013" is quoting the archive and must say so;
tests/test_archive_history.py asserts the separation rather than trusting this
paragraph.

WHY IT IS WORTH DRAWING AT ALL. CME's as-printed daily files floor at 2015, so
the 533 index dates before then exist in no other source. Everything from 2021
on agrees with the as-printed series to pure 2dp rounding, which is what makes
the older, looser years readable as history rather than as noise.

NO STREAMLIT AND NO PLOTLY IN HERE. The two dashboards use different palettes,
so each page draws its own chart from these numbers -- the same split
feeder_futures.py already runs on.
"""

from datetime import date, timedelta

import snowflake_db as db

#: Above this, a year's archive figure and CME's as-printed figure are saying
#: materially different things. Pure 2dp rounding alone reaches 0.0050, so a
#: threshold at 0.01 would flag every year on file and mean nothing; 0.05 is
#: comfortably clear of rounding and still an order of magnitude below the
#: 0.1612 that separates 2020 from the rounding-only years after it.
MATERIAL_GAP = 0.05


def load_series(conn):
    """
    [(iso_date, seven_day_wtd_price, archive_only)], oldest first.

    `archive_only` is True where CME's as-printed table has no row for that
    date -- derived from what cme_ftp_daily actually holds rather than from a
    hard-coded 2015 cutoff, so backfilling more as-printed history narrows the
    archive-only segment by itself instead of leaving a constant lying about
    it.
    """
    printed = {
        str(db.iso(rd))
        for (rd,) in conn.cursor().execute(
            "SELECT report_date FROM cme_ftp_daily "
            "WHERE fci_value IS NOT NULL").fetchall()
    }
    out = []
    for rd, px in conn.cursor().execute(
            "SELECT report_date, seven_day_wtd_price FROM cme_archive_daily "
            "WHERE seven_day_wtd_price IS NOT NULL").fetchall():
        iso = str(db.iso(rd))
        out.append((iso, float(px), iso not in printed))
    out.sort()
    return out


def agreement_by_year(conn):
    """
    [{year, shared_dates, worst_gap, head_mismatches}], oldest first.

    The measurement that says what the archive is good for, and the reason it
    is computed rather than quoted: it is the reader's only basis for deciding
    whether a 2013 figure can be read like a 2025 one. `year` is the calendar
    year of the index date, NOT cme_archive_daily.source_year -- two dates are
    published in the wrong year's workbook (FC2018 carries 2019-04-01, FC2022
    carries 2023-04-03) and bucketing a 2019 date under 2018 would misreport
    the very years this table exists to separate.
    """
    printed = {}
    for rd, v, head in conn.cursor().execute(
            "SELECT report_date, fci_value, total_head FROM cme_ftp_daily "
            "WHERE fci_value IS NOT NULL").fetchall():
        printed[str(db.iso(rd))] = (float(v), head)

    years = {}
    for rd, px, head in conn.cursor().execute(
            "SELECT report_date, seven_day_wtd_price, seven_day_head "
            "FROM cme_archive_daily "
            "WHERE seven_day_wtd_price IS NOT NULL").fetchall():
        iso = str(db.iso(rd))
        if iso not in printed:
            continue
        pv, ph = printed[iso]
        slot = years.setdefault(int(iso[:4]),
                                {"year": int(iso[:4]), "shared_dates": 0,
                                 "worst_gap": 0.0, "head_mismatches": 0})
        slot["shared_dates"] += 1
        slot["worst_gap"] = max(slot["worst_gap"], abs(float(px) - pv))
        if ph is not None and head is not None and abs(float(head) - float(ph)) > 0.5:
            slot["head_mismatches"] += 1
    return [years[y] for y in sorted(years)]


#: A barn silent for this long, measured from the ARCHIVE'S OWN last date and
#: not from today, has left the index. Measured from the data's last date
#: because the archive is a manual backfill: run it in six months' time and a
#: "today" rule would declare every barn gone at once.
GONE_AFTER_DAYS = 365


def barn_key(name):
    """
    One barn, one key. Upper-cased with internal whitespace collapsed.

    THE WORKBOOKS SPELL THE SAME BARN SEVERAL WAYS -- 'ABERDEEN' and
    'aberdeen', three spellings of 'billings pays', 'APACHE  VIDEO' with two
    spaces. 352 raw names in cme_archive_locations are 280 barns. Keyed raw, a
    barn's fourteen-year history splits into fragments and each fragment reads
    as a barn that appeared and vanished -- which is precisely the thing the
    roster below is for, so the error would arrive disguised as the finding.

    Normalisation lives HERE, in Python, and the callers group on its output
    rather than on a TRIM(UPPER(...)) written into SQL. Two normalisations
    that can drift apart is the same bug one level up.
    """
    return " ".join(str(name).upper().split())


def _barn_rows(conn):
    for rd, loc, st, head, wt, px in conn.cursor().execute(
            "SELECT report_date, location, state, head, wtd_weight, wtd_price "
            "FROM cme_archive_locations WHERE head IS NOT NULL").fetchall():
        yield str(db.iso(rd)), barn_key(loc), st, float(head), wt, px


def barn_roster(conn):
    """
    [{key, state, first, last, dates, head, share, gone}], biggest first.

    `gone` is the finding this exists for: a barn that reported for years and
    then stopped. CARTHAGE-JOPLIN ran 2013-01-03 to 2024-05-20 across 364
    dates and 299,783 head, and nothing else in this project would say so.
    """
    acc, total = {}, 0.0
    last_seen = None
    for iso, key, state, head, _wt, _px in _barn_rows(conn):
        slot = acc.setdefault(key, {"key": key, "state": state,
                                    "first": iso, "last": iso,
                                    "dates": 0, "head": 0.0})
        slot["first"] = min(slot["first"], iso)
        if iso >= slot["last"]:
            slot["last"] = iso
            # The state as the barn was last filed under, not as first filed:
            # AMS has moved a barn's state label before now.
            slot["state"] = state or slot["state"]
        slot["dates"] += 1
        slot["head"] += head
        total += head
        last_seen = iso if last_seen is None else max(last_seen, iso)

    if last_seen is None:
        return []
    cutoff = (date.fromisoformat(last_seen)
              - timedelta(days=GONE_AFTER_DAYS)).isoformat()
    out = []
    for slot in acc.values():
        slot["share"] = slot["head"] / total if total else 0.0
        slot["gone"] = slot["last"] < cutoff
        out.append(slot)
    out.sort(key=lambda s: -s["head"])
    return out


def barn_series(conn, key):
    """[(iso, head, weight, price)] for one barn, oldest first."""
    want = barn_key(key)
    rows = [(iso, head, wt, px)
            for iso, k, _st, head, wt, px in _barn_rows(conn)
            if k == want and px is not None]
    rows.sort()
    return rows


def roster_summary(roster, printed_from=None):
    """
    The handful of figures the barn panel quotes, so neither page derives them.

    `printed_from` is the first date CME's as-printed table covers, from
    summary(); barns whose whole life predates it are ones no other source in
    this project has ever held.
    """
    if not roster:
        return None
    gone = [b for b in roster if b["gone"]]
    return {
        "barns": len(roster),
        "gone": len(gone),
        "gone_head": sum(b["head"] for b in gone),
        "gone_share": sum(b["share"] for b in gone),
        "biggest_gone": max(gone, key=lambda b: b["head"]) if gone else None,
        "pre_era_only": len([b for b in roster if printed_from
                             and b["last"] < printed_from]),
    }


def printed_span(series):
    """(first, last) iso date the as-printed table covers, or (None, None)."""
    shared = [s[0] for s in series if not s[2]]
    return (shared[0], shared[-1]) if shared else (None, None)


def holes_in_printed(series):
    """
    Archive dates INSIDE the as-printed era that cme_ftp_daily does not hold.

    These are gaps in our own collection of CME's files, not in CME's
    publishing, and they are the archive's second use: nine of them on file at
    the time of writing, several on Fridays. Distinguished from the two things
    that are NOT holes -- everything before the as-printed era began, which no
    collection could have, and anything after the last as-printed date, which
    is simply not fetched yet and would otherwise report today's pending print
    as a permanent gap every single afternoon.
    """
    first, last = printed_span(series)
    if first is None:
        return []
    return [s[0] for s in series if s[2] and first <= s[0] <= last]


def rounding_era_from(agreement, material=MATERIAL_GAP):
    """
    The earliest year from which EVERY later year agrees to within `material`,
    or None if no such run exists.

    Derived rather than written down as "2021", because that is a fact about
    CME's restating practice and not a constant of this codebase -- if 2020
    were ever reissued cleanly the answer moves on its own, and a hard-coded
    year would then be quietly wrong in the direction of over-claiming.
    """
    tail = None
    for row in reversed(agreement):
        if row["worst_gap"] > material:
            break
        tail = row["year"]
    return tail


def summary(series, agreement):
    """
    The handful of figures the caption quotes, so neither page derives them.

    Returns None for an empty series rather than raising: the archive is a
    manual backfill and a machine that has never run it has no rows, which is
    an empty panel and not an error.
    """
    if not series:
        return None
    printed_from, printed_to = printed_span(series)
    # The segment that exists in no other source: everything BEFORE the
    # as-printed era opens. Not "every archive-only date" -- that also sweeps
    # up the holes below and today's unfetched print, which are different
    # claims about why a date is missing and must not be added together.
    only = [s for s in series if printed_from is None or s[0] < printed_from]
    shared = [s for s in series if not s[2]]
    holes = holes_in_printed(series)
    era = rounding_era_from(agreement)
    recent = [a for a in agreement if era is not None and a["year"] >= era]
    older = [a for a in agreement if era is None or a["year"] < era]
    worst_older = max(older, key=lambda a: a["worst_gap"]) if older else None
    return {
        "first": series[0][0],
        "last": series[-1][0],
        "archive_only_dates": len(only),
        "archive_only_last": only[-1][0] if only else None,
        "printed_from": printed_from,
        "printed_to": printed_to,
        "holes": holes,
        "shared_dates": len(shared),
        "years": len({s[0][:4] for s in series}),
        "rounding_era_from": era,
        "recent_worst": max((a["worst_gap"] for a in recent), default=None),
        "older_worst": worst_older["worst_gap"] if worst_older else None,
        "older_worst_year": worst_older["year"] if worst_older else None,
    }
