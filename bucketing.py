"""
Per-location corrections to the date a sale is BUCKETED into, plus a drift
check that verifies those corrections still hold.

Shared by update_index.py (which buckets when building the rolling window) and
app.py (which must display the same buckets, or the daily rows stop reconciling
with the index beside them). Kept in its own module so neither has to import
the other -- app.py pulling in update_index would drag requests and pdfplumber
into the Streamlit process for two constants.

Nothing here changes the index METHODOLOGY. The sample, the grade and weight
filters, the pound weighting and the 7-calendar-day window are all untouched.
This only decides which day a qualifying sale counts toward, which is the same
kind of adjustment as CME's own two documented rules -- "Saturday and Sunday
sales count as Monday" and "all direct trade reports are considered Friday
transactions" -- both of which also override the true calendar date.

The difference, and it matters: those two are rules CME publishes. What is
below is a pattern OBSERVED in CME's files. That is weaker evidence, which is
why check_bucket_drift() exists.
"""
import re
from datetime import date, datetime, timedelta

import snowflake_db as db

# ---------------------------------------------------------------------------
# CLOVIS NM: MARS reports every Clovis sale on a Wednesday; CME buckets every
# one on the Thursday. Verified 2026-09-09 across 13 consecutive sales, matched
# on identical head count AND identical weighted price, so these are provably
# the same sales rather than coincidences:
#
#     ours (MARS)       CME              head    price
#     2026-08-05 Wed    2026-08-06 Thu     76    $321.44
#     2026-08-12 Wed    2026-08-13 Thu     44    $326.34
#     2026-08-19 Wed    2026-08-20 Thu     73    $325.07
#     2026-08-26 Wed    2026-08-27 Thu    107    $308.06
#     2026-09-02 Wed    2026-09-03 Thu     47    $298.39
#
# 13 of 13 offset by exactly +1, no exceptions, and CME's raw file stamps that
# row's own Sale Date as "9/3/26" -- it is not a parse artefact. Since CME's
# index is what this reconstruction predicts, reproducing it means grouping the
# way CME groups, whatever the true calendar sale date was. Measured on the
# dates with complete inputs (2026-08-28 on): MAE 0.0134 -> 0.0043, with
# 2026-09-02 going from +0.07 against CME to exact.
#
# raw_date always keeps what USDA reported, so the true sale date is never lost.
LOCATION_BUCKET_SHIFT_DAYS = {
    "clovis": 1,
}

# EL RENO OK (OKC West): CME buckets its sale on WEDNESDAY, essentially always
# -- 555 of 563 rows across 2015-01-07..2026-09-02 (99%), while USDA reports the
# sale itself on Tuesday (115 of 122 reports, 94%). Expressed as a weekday snap
# rather than a day count because that is what the evidence actually shows, and
# because it is idempotent: a report already on Wednesday is left alone.
#
# This CORRECTS an earlier decision recorded here as settled. A blanket +1 day
# shift was tried, measured, and rejected because it took 2026-09-02 from +0.07
# to +1.36 and the MAE from 0.0134 to 0.1966. That measurement was right and the
# conclusion drawn from it was wrong: the damage came from DOUBLE-shifting the
# one week in nine where detect_final_sale_day() had already moved the report
# (2026-09-01 Tue -> 09-02 Wed, from its narrative), not from the offset being
# spurious. A weekday snap cannot double-apply, so it fixes the other weeks
# without breaking that one.
#
# What the mis-rejection cost, verified against CME's own published files: our
# buckets agreed with CME on 1 of the last 14 El Reno sales. CME's 09/08 print
# ($327.43 on 9,829 head) contains NO El Reno row, because that Tuesday sale
# belongs to CME's 09/09 index -- so including it on 09/08 moved our estimate to
# $327.7606 against CME's $327.4300, a +0.33 miss, where the frozen pre-shift
# call had matched to +0.0006 on an identical 9,829 head.
LOCATION_BUCKET_WEEKDAY = {
    "el reno": 2,          # 2 = Wednesday (Monday is 0)
}

# Locations whose apparent offset has been investigated and DELIBERATELY not
# corrected. check_bucket_drift() stays silent about these -- a check that
# flagged them every morning would be noise, and noise gets ignored.
BUCKET_SHIFT_REJECTED = {}

# A location needs at least this many matched sales before its offset is worth
# an opinion, and this share of them must agree, before the check says anything.
_MIN_MATCHES = 5
_AGREEMENT = 0.8


def _as_date(v) -> date:
    """
    Coerce whatever a backend hands back into a datetime.date.

    The two backends disagree, and the disagreement is silent: SQLite has no
    DATE type and returns the ISO TEXT it stored, while Snowflake's connector
    returns a real datetime.date (pandas may present either as a Timestamp).
    Assuming str here shipped a TypeError that only fired against Snowflake --
    i.e. only in production -- and load_data() swallows the traceback and calls
    st.stop(), so the entire page went blank rather than one panel. Normalise
    at the boundary, for the same reason snowflake_db.iso() exists.
    """
    if isinstance(v, str):
        return date.fromisoformat(v[:10])       # tolerate a datetime string
    if isinstance(v, datetime):                 # also covers pandas.Timestamp
        return v.date()
    if isinstance(v, date):
        return v
    raise TypeError(f"cannot read {v!r} ({type(v).__name__}) as a date")


def shifted_bucket_date(location, report_date) -> str:
    """
    report_date moved by this location's bucketing correction, if any, as an
    ISO string. Accepts a str, date, datetime or pandas Timestamp -- see
    _as_date() for why that matters.

    Two kinds of correction, and the difference matters:

      LOCATION_BUCKET_WEEKDAY snaps FORWARD to the next occurrence of a fixed
      weekday, and is a no-op if already on it. Idempotent, so it composes
      safely with detect_final_sale_day() having already moved the report.

      LOCATION_BUCKET_SHIFT_DAYS adds a fixed number of days. NOT idempotent --
      applying it on top of another correction double-counts, which is exactly
      how the El Reno rule came to be wrongly rejected. Only use it where the
      target weekday is not stable.
    """
    key = (location or "").strip().lower()
    d = _as_date(report_date)

    target = LOCATION_BUCKET_WEEKDAY.get(key)
    if target is not None:
        return (d + timedelta(days=(target - d.weekday()) % 7)).isoformat()

    n = LOCATION_BUCKET_SHIFT_DAYS.get(key)
    return (d + timedelta(days=n) if n else d).isoformat()


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())[:12]


def _by_location(conn, table, since_iso):
    """{norm_location: [(iso_date, head, weighted_price)]} for one table."""
    rows = conn.cursor().execute(
        f"SELECT report_date, location, SUM(head_count), "
        f"SUM(head_count * avg_weight * avg_price) / SUM(head_count * avg_weight) "
        f"FROM {table} WHERE report_date >= {db.placeholders(1)} "
        f"GROUP BY report_date, location",
        (since_iso,),
    ).fetchall()
    out = {}
    for rd, loc, head, price in rows:
        rd = db.iso(rd)
        out.setdefault(_norm(loc), []).append((rd, int(head), float(price), loc))
    return out


def check_bucket_drift(conn, lookback_days=120):
    """
    Verify the corrections above still describe reality, and notice any new
    location developing a consistent offset.

    Matches our sales against CME's own published per-location rows on identical
    head count AND weighted price, which identifies the same sale regardless of
    how either side dates it, then compares the day offset against what
    LOCATION_BUCKET_SHIFT_DAYS expects.

    Returns a list of human-readable warnings; empty means everything holds.
    Deliberately returns warnings rather than raising: a drifted assumption
    should be loud in the log, not something that aborts the day's refresh.
    """
    since = (date.today() - timedelta(days=lookback_days)).isoformat()
    try:
        ours = _by_location(conn, "mars_sales", since)
        theirs = _by_location(conn, "cme_ftp_locations", since)
    except Exception as e:                       # table missing on this backend
        return [f"bucket drift check skipped: {type(e).__name__}: {e}"]

    warnings = []
    for key in sorted(set(ours) & set(theirs)):
        if key in {_norm(k) for k in BUCKET_SHIFT_REJECTED}:
            continue
        deltas, label = [], None
        for rd, head, price, loc in ours[key]:
            label = label or loc
            for rd2, head2, price2, _ in theirs[key]:
                if head == head2 and abs(price - price2) < 0.015:
                    deltas.append((_as_date(rd2) - _as_date(rd)).days)
                    break
        if len(deltas) < _MIN_MATCHES:
            continue

        # Expected offset per sale, not a constant: a weekday snap's offset
        # depends on which weekday the sale itself fell on.
        expected_for = {}
        for rd, _h, _p, loc in ours[key]:
            expected_for[rd] = (date.fromisoformat(shifted_bucket_date(loc, rd))
                                - date.fromisoformat(rd)).days
        expected = max(set(expected_for.values()), key=list(expected_for.values()).count)             if expected_for else 0
        agreeing = sum(1 for d in deltas if d in set(expected_for.values()))
        if agreeing / len(deltas) >= _AGREEMENT:
            continue                              # behaving as configured

        modal = max(set(deltas), key=deltas.count)
        share = deltas.count(modal) / len(deltas)
        if key in LOCATION_BUCKET_SHIFT_DAYS or key in LOCATION_BUCKET_WEEKDAY:
            warnings.append(
                f"BUCKET DRIFT: {label} is configured to shift {expected:+d}d but "
                f"only {agreeing}/{len(deltas)} recent sales match that; the "
                f"commonest offset is now {modal:+d}d ({share:.0%}). The "
                f"correction in bucketing.py may no longer hold -- re-measure "
                f"before trusting it."
            )
        elif modal != 0 and share >= _AGREEMENT:
            warnings.append(
                f"possible new bucketing offset: {label} shows {modal:+d}d on "
                f"{share:.0%} of {len(deltas)} matched sales and is NOT "
                f"configured. A CANDIDATE ONLY -- measure the effect on "
                f"complete-input dates before adding it. El Reno looked exactly "
                f"like this and correcting it was 10x worse."
            )
    return warnings

if __name__ == "__main__":
    # Dependency-free self-check: `python bucketing.py`.
    #
    # Every type below is one a real backend hands over -- SQLite an ISO
    # string, Snowflake a datetime.date, pandas a Timestamp. Taking str for
    # granted here broke the deployed dashboard outright, because the only
    # code path that reads Snowflake is production and the only path exercised
    # locally is SQLite. Keep all four covered.
    import pandas as _pd

    _WED, _THU = "2026-09-02", "2026-09-03"
    for _v in (_WED,                                  # sqlite: ISO text
               date(2026, 9, 2),                      # snowflake: date
               datetime(2026, 9, 2, 14, 30),          # datetime
               _pd.Timestamp("2026-09-02"),           # pandas
               "2026-09-02T00:00:00"):                # ISO datetime text
        _got = shifted_bucket_date("Clovis", _v)
        assert _got == _THU, f"Clovis {type(_v).__name__} -> {_got}, want {_THU}"
        _got = shifted_bucket_date("Joplin", _v)      # unconfigured: no shift
        assert _got == _WED, f"Joplin {type(_v).__name__} -> {_got}, want {_WED}"

    # The weekday snap, including its idempotence -- the property whose absence
    # caused the original blanket +1 shift to double-apply and be rejected.
    assert shifted_bucket_date("El Reno", "2026-09-08") == "2026-09-09"   # Tue -> Wed
    assert shifted_bucket_date("El Reno", "2026-09-09") == "2026-09-09"   # already Wed
    assert shifted_bucket_date("El Reno", "2026-09-02") == "2026-09-02"   # Wed, no-op
    assert shifted_bucket_date("El Reno", "2026-09-07") == "2026-09-09"   # Mon -> Wed
    assert shifted_bucket_date("El Reno", "2026-09-10") == "2026-09-16"   # Thu -> next Wed
    # Snapping twice must equal snapping once.
    _once = shifted_bucket_date("El Reno", "2026-09-08")
    assert shifted_bucket_date("El Reno", _once) == _once

    # Case and surrounding whitespace must not decide whether a shift applies.
    for _name in ("clovis", "CLOVIS", "  Clovis  "):
        assert shifted_bucket_date(_name, _WED) == _THU, _name
    # A missing location must not raise -- mars_sales allows it.
    assert shifted_bucket_date(None, _WED) == _WED
    assert shifted_bucket_date("", _WED) == _WED

    print("bucketing self-check passed: %d configured shift(s), %d rejected"
          % (len(LOCATION_BUCKET_SHIFT_DAYS), len(BUCKET_SHIFT_REJECTED)))
