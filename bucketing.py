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
from datetime import date, timedelta

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

# Locations whose apparent offset has been investigated and DELIBERATELY not
# corrected. check_bucket_drift() stays silent about these -- a check that
# flagged them every morning would be noise, and noise gets ignored.
BUCKET_SHIFT_REJECTED = {
    "el reno": (
        "Flagged as offset on 12 of 13 sales -- apparently a stronger pattern "
        "than Clovis -- but shifting it takes 2026-09-02 from +0.07 to +1.36 "
        "and the MAE from 0.0134 to 0.1966. El Reno is ALREADY corrected by "
        "detect_final_sale_day(), which moves its multi-day sales to their true "
        "final day from the report narrative and fires on 1 week in 9; a "
        "blanket shift applies that a second time on the 8 weeks needing "
        "nothing. The matcher below cannot tell 'CME buckets this later' from "
        "'the matcher paired the wrong two sales', so a detected pattern is a "
        "hypothesis to measure, never a licence to act."
    ),
}

# A location needs at least this many matched sales before its offset is worth
# an opinion, and this share of them must agree, before the check says anything.
_MIN_MATCHES = 5
_AGREEMENT = 0.8


def shifted_bucket_date(location, report_date_iso: str) -> str:
    """report_date_iso moved by this location's bucketing correction, if any."""
    n = LOCATION_BUCKET_SHIFT_DAYS.get((location or "").strip().lower())
    if not n:
        return report_date_iso
    return (date.fromisoformat(report_date_iso) + timedelta(days=n)).isoformat()


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
                    deltas.append((date.fromisoformat(rd2) - date.fromisoformat(rd)).days)
                    break
        if len(deltas) < _MIN_MATCHES:
            continue

        expected = LOCATION_BUCKET_SHIFT_DAYS.get(key, 0)
        agreeing = sum(1 for d in deltas if d == expected)
        if agreeing / len(deltas) >= _AGREEMENT:
            continue                              # behaving as configured

        modal = max(set(deltas), key=deltas.count)
        share = deltas.count(modal) / len(deltas)
        if key in LOCATION_BUCKET_SHIFT_DAYS:
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
