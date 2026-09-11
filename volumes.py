"""
Index volume: how much cattle is behind the index, against last week, last
year, and the five- and ten-year seasonal averages.

Everything here reads CME's OWN published numbers, never our reconstruction.
Our mars_sales cannot carry history: direct trade exists only from 2026-08-28
and video/internet from 2026-08-05, so our head counts before late August 2026
are missing whole components and a year-over-year figure off them would measure
our data collection rather than the cattle market. Our estimate appears only
for dates CME has not printed, and is flagged when it does.

TWO CME SERIES, USED FOR DIFFERENT THINGS:

  cme_ftp_daily.total_head is the 7-DAY ROLLING window -- the head behind the
  index on that date. Right for point-in-time comparisons. Adding it across a
  year would count every animal about five times, so it is never summed.

  cme_ftp_daily.same_day_head is CME's DAILY TOTALS row -- that date's own
  sales, non-overlapping. Right for cumulative totals.

The per-location table (cme_ftp_locations) is deliberately NOT used for
cumulative volume, despite also being same-day. Measured against CME's own
DAILY TOTALS, our per-location parse captures only 93-95% of head in 2015-2020
against ~99% from 2023 on -- the older fixed-width layouts drop rows (e.g.
2018-12-28 line 21 runs the date into the name, "12/28/18NEW MEXICO DIRECT",
and the row is lost). Summing it would understate older years by 5-7% and make
the current year look better than it is against them. same_day_head is one
robustly-parsed number per date and has no such gradient.

ALIGNMENT, which changes answers:

  Volume is violently seasonal -- the median window runs ~46,500 head in
  January against ~16,500 in July -- so comparisons must be seasonally matched.

  CME publishes weekdays only, and Monday windows run far heavier than Friday
  ones, so the year-ago point steps back 364 days (52 weeks) rather than 365.

  Cumulative cuts align on ISO week AND ISO weekday. Cutting on week alone
  compared 2026 through Tuesday of week 37 against a COMPLETE week 37 in prior
  years -- 179 dates against 185 -- which showed 2026 at -0.2% versus 2025 when
  the properly aligned figure is +1.4%. The sign was wrong, not just the
  magnitude.
"""
from datetime import date, timedelta
from statistics import median

import snowflake_db as db

# Comparison periods. Both end at the last complete year so the current year is
# measured against history rather than partly against itself.
PERIODS = {"5yr": (2021, 2025), "10yr": (2016, 2025)}

# A comparison date CME did not publish (holiday, or an archive gap) falls back
# to the nearest earlier published date within this many days; beyond that the
# comparison is reported missing rather than stretched.
NEAREST_DAYS = 3

# 52 weeks, not a calendar year: keeps the weekday aligned.
YEAR_AGO_DAYS = 364

# A week's norm is only meaningful if the pooled years agree reasonably well.
# ISO weeks 1 and 52 straddle the New Year shutdown and mix closed days with
# normal ones, so their interquartile spread runs far wider than an ordinary
# week's ~33%. Quoting a confident percentage off that would be false precision.
NORM_MAX_SPREAD = 0.75

# Cumulative totals scale with how many days a year published, not only with
# how many cattle sold. Above this gap in date counts, say so.
YTD_MAX_DATE_GAP = 0.05


# ---------------------------------------------------------------- window view

def load_series(conn):
    """
    {iso_date: (window_head, is_estimate)} -- CME's published 7-day window head,
    with our reconstruction only for dates CME has not printed.

    Weekend index dates from our own table are dropped: CME does not publish
    them (Saturday and Sunday sales count as Monday), so they would sit in the
    series with nothing in history to compare against.
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
        if iso in out or date.fromisoformat(iso).weekday() >= 5:
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


def _norm_for(series, target: date, first_year, last_year):
    """(median, p25, p75, n) for target's ISO week over a year range."""
    wk = target.isocalendar()[1]
    vals = []
    for iso, (head, est) in series.items():
        if est:
            continue                      # never let our estimate into history
        d = date.fromisoformat(iso)
        if first_year <= d.year <= last_year and d.isocalendar()[1] == wk:
            vals.append(head)
    if len(vals) < 5:
        return None, None, None, len(vals)
    vals.sort()
    q = lambda p: vals[min(len(vals) - 1, int(p * len(vals)))]
    return median(vals), q(0.25), q(0.75), len(vals)


def compare(conn, index_date_iso=None):
    """
    Window head for an index date against last week, last year, and each
    seasonal norm in PERIODS.

    Percentages are None where the basis is missing, never zero -- a missing
    baseline and an unchanged one are different facts.
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
    pct = lambda base: (100.0 * (head - base) / base) if base else None

    out = {
        "date": used, "head": head, "is_estimate": series[used][1],
        "week_ago": wk_head, "week_ago_date": wk_used, "week_pct": pct(wk_head),
        "year_ago": yr_head, "year_ago_date": yr_used, "year_pct": pct(yr_head),
        "iso_week": target.isocalendar()[1], "norms": {},
    }
    for key, (y0, y1) in PERIODS.items():
        norm, p25, p75, n = _norm_for(series, target, y0, y1)
        out["norms"][key] = {
            "label": f"{y1 - y0 + 1}-Yr", "years": (y0, y1),
            "norm": norm, "p25": p25, "p75": p75, "n": n, "pct": pct(norm),
            "reliable": bool(norm and p25 is not None
                             and (p75 - p25) / norm <= NORM_MAX_SPREAD),
            "spread_pct": (100.0 * (p75 - p25) / norm) if norm else None,
        }
    return out


# ------------------------------------------------------------ cumulative view

def ytd(conn, index_date_iso=None):
    """
    Cumulative head through an index date's point in the week, against last
    year and each period average.

    Sums same_day_head (CME's DAILY TOTALS), which is non-overlapping. Cuts
    every year at the same (ISO week, ISO weekday) so a partial current week is
    not compared against complete ones -- see the module note for what that
    error did to the sign.
    """
    rows = conn.cursor().execute(
        "SELECT report_date, same_day_head FROM cme_ftp_daily "
        "WHERE same_day_head IS NOT NULL").fetchall()
    parsed = [(date.fromisoformat(str(db.iso(rd))), int(h)) for rd, h in rows]
    if not parsed:
        return {"head": None}

    target = (date.fromisoformat(index_date_iso) if index_date_iso
              else max(d for d, _ in parsed))
    cut = (target.isocalendar()[1], target.isocalendar()[2])

    agg = {}
    for d, h in parsed:
        if (d.isocalendar()[1], d.isocalendar()[2]) <= cut:
            tot, n = agg.get(d.year, (0, 0))
            agg[d.year] = (tot + h, n + 1)

    year = target.year
    cur = agg.get(year)
    if not cur:
        return {"head": None}
    prev = agg.get(year - 1)

    out = {"iso_week": cut[0], "iso_weekday": cut[1], "year": year,
           "head": cur[0], "dates": cur[1], "prev_year": year - 1,
           "prev_head": prev[0] if prev else None,
           "prev_dates": prev[1] if prev else None,
           "periods": {}}

    if prev:
        out["prev_pct"] = 100.0 * (cur[0] - prev[0]) / prev[0]
        gap = abs(cur[1] - prev[1]) / max(cur[1], prev[1])
        out["dates_comparable"] = gap <= YTD_MAX_DATE_GAP
        out["date_gap_pct"] = 100.0 * gap
    else:
        out["prev_pct"] = out["dates_comparable"] = out["date_gap_pct"] = None

    for key, (y0, y1) in PERIODS.items():
        vals = [agg[y] for y in range(y0, y1 + 1) if y in agg]
        if not vals:
            out["periods"][key] = {"label": f"{y1 - y0 + 1}-Yr", "avg": None,
                                   "oly_avg": None}
            continue
        avg = sum(h for h, _ in vals) / len(vals)
        entry = {
            "label": f"{y1 - y0 + 1}-Yr", "years": (y0, y1), "n": len(vals),
            "avg": avg, "avg_dates": sum(n for _, n in vals) / len(vals),
            "pct": 100.0 * (cur[0] - avg) / avg,
            "oly_avg": None,
        }
        entry.update(_olympic(agg, range(y0, y1 + 1), cur[0]))
        out["periods"][key] = entry
    return out


def _olympic(agg, years, current):
    """
    Olympic average: drop the highest and lowest year, average the rest. The
    usual ag-benchmarking remedy for one freak season dominating a mean.

    Reported ALONGSIDE the plain average, never instead of it, because on this
    particular series the assumption behind it does not hold. Olympic averaging
    treats extremes as noise. Feeder volume has been trending DOWN -- 2025 is
    the lowest year in both the five- and ten-year windows -- so "drop the
    lowest" removes the most recent and most relevant observation and raises the
    baseline. Measured 2026-09-10: it moves 2026 from -9.96% to -10.99% against
    five years and -13.64% to -14.12% against ten. Small, but in the direction
    that flatters the baseline rather than the current year, and for a reason
    that is trend rather than outlier.
    """
    pairs = sorted((agg[y][0], y) for y in years if y in agg)
    if len(pairs) < 3:
        return {"oly_avg": None}
    low, high, kept = pairs[0], pairs[-1], pairs[1:-1]
    oly = sum(v for v, _ in kept) / len(kept)
    return {
        "oly_avg": oly,
        "oly_pct": 100.0 * (current - oly) / oly,
        "oly_n": len(kept),
        "oly_dropped_low": low[1], "oly_dropped_low_head": low[0],
        "oly_dropped_high": high[1], "oly_dropped_high_head": high[0],
    }


def history(conn, years=(2026, 2025)):
    """
    {year: [(iso_week, window_head)]} plus a per-week band from the LONGEST
    configured period. Published data only, so the chart never mixes measured
    history with a forecast.
    """
    series = load_series(conn)
    y0 = min(p[0] for p in PERIODS.values())
    y1 = max(p[1] for p in PERIODS.values())
    byyear = {y: [] for y in years}
    band = {}
    for iso, (head, est) in sorted(series.items()):
        d = date.fromisoformat(iso)
        wk = d.isocalendar()[1]
        if d.year in byyear and not est:
            byyear[d.year].append((wk, head))
        if not est and y0 <= d.year <= y1:
            band.setdefault(wk, []).append(head)
    norm = {}
    for wk, vals in band.items():
        if len(vals) < 5:
            continue
        vals.sort()
        q = lambda p: vals[min(len(vals) - 1, int(p * len(vals)))]
        norm[wk] = (median(vals), q(0.25), q(0.75))
    return byyear, norm, (y0, y1)


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    conn = db.get_conn()
    f = lambda v: f"{v:,.0f}" if v is not None else "n/a"
    p = lambda v: f"{v:+.1f}%" if v is not None else "n/a"

    c = compare(conn)
    print(f"WINDOW  {c['date']} (ISO week {c['iso_week']})"
          f"{'  [OUR ESTIMATE]' if c['is_estimate'] else ''}")
    print(f"   head           {f(c['head'])}")
    print(f"   vs last week   {f(c['week_ago'])} on {c['week_ago_date']}   {p(c['week_pct'])}")
    print(f"   vs last year   {f(c['year_ago'])} on {c['year_ago_date']}   {p(c['year_pct'])}")
    for k, nm in c["norms"].items():
        flag = "" if nm["reliable"] else f"   [UNRELIABLE: spread {nm['spread_pct']:.0f}%]"
        print(f"   vs {nm['label']:<6} norm  {f(nm['norm'])} "
              f"(p25 {f(nm['p25'])}-{f(nm['p75'])}, n={nm['n']})   {p(nm['pct'])}{flag}")

    y = ytd(conn)
    print(f"\nCUMULATIVE through ISO week {y['iso_week']} day {y['iso_weekday']}")
    print(f"   {y['year']}           {f(y['head'])} over {y['dates']} dates")
    print(f"   {y['prev_year']}           {f(y['prev_head'])} over {y['prev_dates']} dates"
          f"   {p(y['prev_pct'])}   (dates comparable: {y['dates_comparable']},"
          f" gap {y['date_gap_pct']:.1f}%)")
    for k, pr in y["periods"].items():
        if pr["avg"] is None:
            continue
        print(f"   {pr['label']:<6} avg   {f(pr['avg'])} over {pr['avg_dates']:.0f} dates"
              f" ({pr['years'][0]}-{pr['years'][1]}, n={pr['n']})   {p(pr['pct'])}")
    conn.close()
