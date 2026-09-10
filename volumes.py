"""
Index volume: how much cattle is behind the index, against last week, last
year, and the eleven-year seasonal norm.

SOURCE IS CME'S OWN PUBLISHED HEAD COUNT, not our reconstruction, and that is
the load-bearing decision here. cme_ftp_daily.total_head runs 2015-01-01 to
present across ~3,020 dates. Our own mars_sales cannot do history at all: the
direct-trade component exists only from 2026-08-28 and video/internet from
2026-08-05, so our head counts before late August 2026 are missing two whole
components. A year-over-year comparison against our own numbers would measure
our data collection rather than the cattle market. Our estimate is used ONLY
for dates CME has not printed yet, and is flagged when it is.

ALIGNMENT MATTERS MORE THAN IT LOOKS. Two facts force it:

  Volume is violently seasonal -- the 2015-2025 median window head runs 46,516
  in January against 16,530 in July, nearly 3x. Comparing across months is
  meaningless.

  CME publishes weekdays only (Mon-Fri ~600 dates each over eleven years, three
  stray weekend dates). So the year-ago comparison uses -364 days, exactly 52
  weeks, which preserves the weekday. -365 would slide the comparison onto a
  different weekday, and Monday windows are much heavier than Friday ones.

The seasonal norm uses ISO week rather than calendar date, so the fall run
lines up year to year instead of drifting.
"""
from datetime import date, timedelta
from statistics import median

import snowflake_db as db

# Years pooled for the seasonal norm. 2026 is excluded so the current year is
# compared against history rather than against itself.
NORM_FIRST_YEAR = 2015
NORM_LAST_YEAR = 2025

# A comparison date that CME did not publish (holiday, or a gap in the archive)
# falls back to the nearest earlier published date within this many days. Beyond
# that the comparison is reported as unavailable rather than stretched.
NEAREST_DAYS = 3

# 52 weeks, not a calendar year: keeps the weekday aligned. See the module note.
YEAR_AGO_DAYS = 364

# A week's norm is only meaningful if the pooled years agree reasonably well.
# ISO weeks 1 and 52 straddle the New Year shutdown and mix holiday-thin dates
# with normal ones, so their interquartile spread runs ~138% of the median
# (week 1: median 16,578, p25 8,041, p75 30,993) against ~33% for an ordinary
# week. Quoting "-59% vs norm" off a baseline that spans 8,041 to 30,993 would
# read as precision where there is none, so those weeks are flagged instead.
NORM_MAX_SPREAD = 0.75


def load_series(conn):
    """
    {iso_date: (head, is_estimate)} -- CME's published head where it exists,
    our reconstruction only for dates CME has not printed.

    Weekend index dates from our own table are dropped: CME does not publish
    them (Saturday and Sunday sales count as Monday), so including them would
    put rows in the series that no historical comparison can match.
    """
    out = {}
    for rd, head in conn.cursor().execute(
            "SELECT report_date, total_head FROM cme_ftp_daily "
            "WHERE total_head IS NOT NULL").fetchall():
        out[str(db.iso(rd))] = (int(head), False)

    for rd, head in conn.cursor().execute(
            "SELECT report_date, total_head FROM fci_daily "
            "WHERE total_head IS NOT NULL").fetchall():
        iso = str(db.iso(rd))
        if iso in out:
            continue
        if date.fromisoformat(iso).weekday() >= 5:
            continue
        out[iso] = (int(head), True)
    return out


def _at(series, target: date):
    """(head, iso_date_used) at or just before target, or (None, None)."""
    for back in range(NEAREST_DAYS + 1):
        iso = (target - timedelta(days=back)).isoformat()
        if iso in series:
            return series[iso][0], iso
    return None, None


def seasonal_norm(series, target: date):
    """
    (median, p25, p75, n) for target's ISO week across NORM_FIRST..LAST_YEAR.

    Pooling a whole ISO week rather than a single date is deliberate: one
    year-ago date is noisy, and holiday placement drifts between years, so a
    week's worth of dates per year gives a baseline that a single Labor Day
    shift cannot swing.
    """
    wk = target.isocalendar()[1]
    vals = []
    for iso, (head, est) in series.items():
        if est:
            continue                     # never let our own estimate into history
        d = date.fromisoformat(iso)
        if NORM_FIRST_YEAR <= d.year <= NORM_LAST_YEAR and d.isocalendar()[1] == wk:
            vals.append(head)
    if len(vals) < 5:
        return None, None, None, len(vals)
    vals.sort()
    q = lambda p: vals[min(len(vals) - 1, int(p * len(vals)))]
    return median(vals), q(0.25), q(0.75), len(vals)


def compare(conn, index_date_iso=None):
    """
    Volume for an index date against last week, last year and the seasonal norm.

    Percentages are None where the comparison basis is missing, never zero --
    a missing baseline and an unchanged one are different facts.
    """
    series = load_series(conn)
    if index_date_iso is None:
        index_date_iso = max(series)
    target = date.fromisoformat(index_date_iso)

    head, used = _at(series, target)
    if head is None:
        return {"date": index_date_iso, "head": None}

    wk_head, wk_used = _at(series, target - timedelta(days=7))
    yr_head, yr_used = _at(series, target - timedelta(days=YEAR_AGO_DAYS))
    norm, p25, p75, n = seasonal_norm(series, target)

    pct = lambda base: (100.0 * (head - base) / base) if base else None
    return {
        "date": used,
        "head": head,
        "is_estimate": series[used][1],
        "week_ago": wk_head, "week_ago_date": wk_used, "week_pct": pct(wk_head),
        "year_ago": yr_head, "year_ago_date": yr_used, "year_pct": pct(yr_head),
        "norm": norm, "norm_p25": p25, "norm_p75": p75, "norm_n": n,
        "norm_pct": pct(norm),
        "norm_reliable": bool(
            norm and p25 is not None and (p75 - p25) / norm <= NORM_MAX_SPREAD),
        "norm_spread_pct": (100.0 * (p75 - p25) / norm) if norm else None,
        "iso_week": target.isocalendar()[1],
    }


# A YTD comparison is only honest if both years published a comparable number
# of dates -- a year with more publication days totals more head for that reason
# alone. Beyond this fraction of difference the comparison is flagged. 2025 and
# 2026 both had exactly 190 dates through ISO week 37, so the current headline
# is clean; earlier years run 182-204 and need the caveat.
YTD_MAX_DATE_GAP = 0.05


def ytd(conn, index_date_iso=None):
    """
    Year-to-date head through an index date's ISO week, against the same week
    last year and the eleven-year average.

    Sums cme_ftp_locations, which is SAME-DAY constituents -- verified against
    our own same_day_head (2026-09-08 -> 690, 09-04 -> 3,104, 09-03 -> 3,906,
    09-02 -> 2,129, all exact). That distinction is the whole reason this
    function exists rather than summing cme_ftp_daily.total_head: total_head is
    a 7-DAY ROLLING window, so adding it across a year counts every animal
    about five times over and the total is meaningless.

    Also reports the date count per year, because a YTD total scales with how
    many days a year published, not only with how many cattle sold.
    """
    rows = conn.cursor().execute(
        "SELECT report_date, SUM(head_count) FROM cme_ftp_locations "
        "WHERE head_count IS NOT NULL GROUP BY report_date").fetchall()

    if index_date_iso is None:
        index_date_iso = max(str(db.iso(r[0])) for r in rows)
    target = date.fromisoformat(index_date_iso)
    wk = target.isocalendar()[1]

    per_year = {}
    for rd, head in rows:
        d = date.fromisoformat(str(db.iso(rd)))
        if d.isocalendar()[1] > wk:
            continue
        tot, n = per_year.get(d.year, (0, 0))
        per_year[d.year] = (tot + int(head), n + 1)

    cur_year = target.year
    cur = per_year.get(cur_year)
    prev = per_year.get(cur_year - 1)
    hist = [v for y, v in per_year.items()
            if NORM_FIRST_YEAR <= y <= NORM_LAST_YEAR and y != cur_year]

    out = {"iso_week": wk, "year": cur_year,
           "head": cur[0] if cur else None, "dates": cur[1] if cur else None,
           "prev_head": prev[0] if prev else None,
           "prev_dates": prev[1] if prev else None,
           "prev_year": cur_year - 1}
    if cur and prev:
        out["prev_pct"] = 100.0 * (cur[0] - prev[0]) / prev[0]
        gap = abs(cur[1] - prev[1]) / max(cur[1], prev[1])
        out["dates_comparable"] = gap <= YTD_MAX_DATE_GAP
        out["date_gap_pct"] = 100.0 * gap
    else:
        out["prev_pct"] = None
        out["dates_comparable"] = None
        out["date_gap_pct"] = None
    if hist:
        out["hist_head"] = sum(h for h, _ in hist) / len(hist)
        out["hist_years"] = len(hist)
        out["hist_pct"] = (100.0 * (cur[0] - out["hist_head"]) / out["hist_head"]
                           if cur else None)
    else:
        out["hist_head"] = out["hist_years"] = out["hist_pct"] = None
    return out


def history(conn, years=(2026, 2025), weeks_back=None):
    """
    {year: [(iso_week, head)]} for charting, plus a norm band per ISO week.
    Published data only -- our estimate is excluded so the chart never mixes
    measured history with a forecast.
    """
    series = load_series(conn)
    byyear = {y: [] for y in years}
    band = {}
    for iso, (head, est) in sorted(series.items()):
        d = date.fromisoformat(iso)
        wk = d.isocalendar()[1]
        if d.year in byyear and not est:
            byyear[d.year].append((wk, head))
        if not est and NORM_FIRST_YEAR <= d.year <= NORM_LAST_YEAR:
            band.setdefault(wk, []).append(head)
    norm = {}
    for wk, vals in band.items():
        if len(vals) < 5:
            continue
        vals.sort()
        q = lambda p: vals[min(len(vals) - 1, int(p * len(vals)))]
        norm[wk] = (median(vals), q(0.25), q(0.75))
    return byyear, norm


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    conn = db.get_conn()
    series = load_series(conn)
    print(f"series: {len(series)} dates, "
          f"{sum(1 for v in series.values() if v[1])} from our estimate")
    for d in (None, "2026-09-08", "2026-09-04", "2026-07-15", "2026-01-15"):
        c = compare(conn, d)
        if c["head"] is None:
            print(f"\n{d}: no data")
            continue
        f = lambda v: f"{v:,}" if v is not None else "n/a"
        p = lambda v: f"{v:+.1f}%" if v is not None else "n/a"
        print(f"\n{c['date']} (ISO week {c['iso_week']})"
              f"{'  [OUR ESTIMATE]' if c['is_estimate'] else ''}")
        print(f"   head            {f(c['head'])}")
        print(f"   vs last week    {f(c['week_ago'])} on {c['week_ago_date']}"
              f"   {p(c['week_pct'])}")
        print(f"   vs last year    {f(c['year_ago'])} on {c['year_ago_date']}"
              f"   {p(c['year_pct'])}")
        print(f"   vs 11-yr norm   {f(c['norm'])} (p25 {f(c['norm_p25'])} - "
              f"p75 {f(c['norm_p75'])}, n={c['norm_n']})   {p(c['norm_pct'])}"
              f"{'' if c['norm_reliable'] else '   [UNRELIABLE: years spread %.0f%% of the median]' % c['norm_spread_pct']}")

    y = ytd(conn)
    print("")
    print(f"--- YTD through ISO week {y['iso_week']} ---")
    print(f"   {y['year']}   {y['head']:,} head over {y['dates']} dates")
    print(f"   {y['prev_year']}   {y['prev_head']:,} head over {y['prev_dates']} dates"
          f"   -> {y['prev_pct']:+.1f}%")
    print(f"   dates comparable: {y['dates_comparable']} "
          f"(gap {y['date_gap_pct']:.1f}%)")
    print(f"   {y['hist_years']}-year average {y['hist_head']:,.0f}"
          f"   -> {y['hist_pct']:+.1f}%")
    conn.close()
