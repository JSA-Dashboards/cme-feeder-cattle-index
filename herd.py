"""
US cow herd analytics: is the breeding herd expanding or liquidating?

Read-only, and deliberately free of `requests` so the dashboard can import it
without dragging the ingest's HTTP stack into a Streamlit process. Ingest lives
in replacement_reports.py; everything here reads what that stored.

WHY A PRICE RATIO IS THE HEADLINE. The definitive herd numbers -- beef cows and
beef replacement heifers -- are NASS January 1 inventory, annual. For ten months
of the year any rebuilding view runs on proxies, so the proxy had better be a
good one. A bred female is worth either what a neighbour will pay for her bred
or what the packer will pay for her by the pound, and the ratio between those
two IS the retention decision, priced weekly by the people making it.

    year   bred $/hd  salvage $/hd  ratio
    2020         889           661   1.37
    2021         909           731   1.25
    2022       1,021           842   1.21
    2023       1,329         1,100   1.22
    2024       1,770         1,470   1.21
    2025       2,388         1,778   1.32
    2026       2,874         2,028   1.44

READ THE RATIO, NOT THE PREMIUM. Both sides roughly tripled over this span, so
the dollar premium ($233 to $848) mostly tracks the price level. The ratio
controls for that and still rose from 1.21 to 1.44.

AND CHECK WHICH SIDE MOVED. The ratio rises either because bred values rise or
because salvage falls, and those mean opposite things. 2020's 1.37 came from
depressed salvage ($661, lowest in the series) -- packers stopped paying, not
producers wanting cows. 2026's 1.44 has both sides up with bred up faster,
+20% against +14%. Hence decompose() below, which reports the split rather than
leaving a reader to assume.
"""
from datetime import date, timedelta
from statistics import median

import snowflake_db as db

# AMS renamed its price units in 2022: per-animal rows were "Per Head" and are
# now "Per Unit". Honouring only the current label drops every 2020-2021 report
# -- 22,709 rows, the first two years of the only history this source has.
PER_HEAD_UNITS = ("Per Unit", "Per Head")

# A "Per Family" price is a cow AND her calf, so it is never averaged alongside
# a single bred female. Pairs get their own line instead.
PER_PAIR_UNITS = ("Per Family",)

BRED_CLASSES = ("Bred Cows", "Bred Heifers")
PAIR_CLASSES = ("Cow-Calf Pairs", "Heifer Pairs")

# Baseline years for "normal". Matches the 5-year volume norm on the FCI page so
# the two dashboards do not quietly disagree about what normal means.
BASELINE_YEARS = (2021, 2025)

# The current read is a trailing window, not a single sale. One report can swing
# the ratio 25 points on quality mix alone -- 8/18/2026 printed 2.09 off a
# 1,202-head bred special while 9/10 printed 1.38 off 44 head -- so a headline
# built on the latest date would be noise dressed as a signal.
CURRENT_WEEKS = 4


def _rows(conn, since_iso=None):
    where = f"WHERE report_date >= {db.placeholders(1)}" if since_iso else ""
    args = (since_iso,) if since_iso else ()
    return conn.cursor().execute(
        f"SELECT report_date, commodity, class_desc, price_unit, head_count, "
        f"avg_weight, avg_price, age, receipts, receipts_year_ago "
        f"FROM replacement_sales {where}", args).fetchall()


def latest_date(conn):
    r = conn.cursor().execute(
        "SELECT MAX(report_date) FROM replacement_sales").fetchone()
    return str(db.iso(r[0])) if r and r[0] else None


def retention_incentive(conn, since_iso=None):
    """
    Per report date: bred value per head, slaughter-cow salvage per head, and
    the ratio between them.

    Salvage is converted to a per-head basis (Per Cwt x weight / 100) because
    bred females trade per head and slaughter cows per hundredweight. Comparing
    them unconverted is the single easiest way to produce nonsense here.
    """
    per_date = {}
    for (rd, commodity, cls, unit, head, wt, price, _age, _r, _ry) in _rows(conn, since_iso):
        iso = str(db.iso(rd))
        d = per_date.setdefault(iso, {"bred_head": 0, "bred_dollars": 0.0,
                                      "salv_head": 0, "salv_dollars": 0.0})
        if (commodity == "Replacement Cattle" and cls in BRED_CLASSES
                and unit in PER_HEAD_UNITS and price):
            d["bred_head"] += head
            d["bred_dollars"] += head * price
        elif (commodity == "Slaughter Cattle" and cls == "Cows"
              and unit == "Per Cwt" and price and wt):
            d["salv_head"] += head
            d["salv_dollars"] += head * price * wt / 100.0

    out = []
    for iso in sorted(per_date):
        d = per_date[iso]
        if not (d["bred_head"] and d["salv_head"]):
            continue
        bred = d["bred_dollars"] / d["bred_head"]
        salv = d["salv_dollars"] / d["salv_head"]
        out.append({"date": iso, "bred": bred, "salvage": salv,
                    "premium": bred - salv, "ratio": bred / salv,
                    "bred_head": d["bred_head"], "salvage_head": d["salv_head"]})
    return out


def decompose(conn):
    """
    Current retention incentive against the baseline, split by which side moved.

    A rising ratio means opposite things depending on the cause, so this reports
    the bred and salvage changes separately rather than only their quotient.
    """
    inc = retention_incentive(conn)
    if not inc:
        return None
    latest = inc[-1]["date"]
    cutoff = (date.fromisoformat(latest) - timedelta(weeks=CURRENT_WEEKS)).isoformat()
    cur = [r for r in inc if r["date"] > cutoff]
    base = [r for r in inc
            if BASELINE_YEARS[0] <= int(r["date"][:4]) <= BASELINE_YEARS[1]]
    if not cur or not base:
        return None

    prior_year = str(int(latest[:4]) - 1)
    yr = [r for r in inc if r["date"][:4] == prior_year]

    med = lambda rows, k: median(r[k] for r in rows)
    out = {
        "latest": latest, "weeks": CURRENT_WEEKS, "n_current": len(cur),
        "bred": med(cur, "bred"), "salvage": med(cur, "salvage"),
        "premium": med(cur, "premium"), "ratio": med(cur, "ratio"),
        "base_ratio": med(base, "ratio"), "base_years": BASELINE_YEARS,
        "n_base": len(base),
        "bred_head": sum(r["bred_head"] for r in cur),
    }
    out["ratio_vs_base"] = out["ratio"] - out["base_ratio"]
    if yr:
        out["bred_yoy_pct"] = 100.0 * (out["bred"] - med(yr, "bred")) / med(yr, "bred")
        out["salvage_yoy_pct"] = 100.0 * (out["salvage"] - med(yr, "salvage")) / med(yr, "salvage")
        out["prior_year"] = prior_year
        # Which side is driving it. Both up with bred faster is expansion
        # demand; salvage falling is packers retreating, which looks identical
        # in the ratio and means something else entirely.
        b, s = out["bred_yoy_pct"], out["salvage_yoy_pct"]
        if b > 0 and s > 0:
            out["driver"] = ("bred values rising faster than salvage"
                             if b > s else "salvage rising faster than bred values")
        elif s < 0 <= b:
            out["driver"] = "bred values up while salvage falls"
        elif b < 0 and s < 0:
            out["driver"] = ("both falling, salvage faster"
                             if s < b else "both falling, bred faster")
        else:
            out["driver"] = "bred values falling while salvage rises"
    return out


def annual_ratio(conn):
    """[(year, median ratio, median bred, median salvage, n dates)] for charting."""
    inc = retention_incentive(conn)
    by = {}
    for r in inc:
        by.setdefault(r["date"][:4], []).append(r)
    return [(y, median(x["ratio"] for x in v), median(x["bred"] for x in v),
             median(x["salvage"] for x in v), len(v)) for y, v in sorted(by.items())]


def monthly_ratio(conn):
    """{'YYYY-MM': median ratio} -- the series behind the trend chart."""
    inc = retention_incentive(conn)
    by = {}
    for r in inc:
        by.setdefault(r["date"][:7], []).append(r["ratio"])
    return {k: median(v) for k, v in sorted(by.items())}


def class_prices(conn, weeks=CURRENT_WEEKS):
    """
    Per class: current trailing average price and the year-ago comparison.

    Per-head and per-pair classes are reported separately and never blended --
    a pair price includes a calf.
    """
    latest = latest_date(conn)
    if not latest:
        return []
    end = date.fromisoformat(latest)
    cur_lo = (end - timedelta(weeks=weeks)).isoformat()
    yr_hi = (end - timedelta(days=364)).isoformat()
    yr_lo = (end - timedelta(days=364) - timedelta(weeks=weeks)).isoformat()

    buckets = {}
    for (rd, commodity, cls, unit, head, _wt, price, _age, _r, _ry) in _rows(conn):
        iso = str(db.iso(rd))
        if commodity != "Replacement Cattle" or not price or not head:
            continue
        if unit in PER_HEAD_UNITS:
            basis = "per head"
        elif unit in PER_PAIR_UNITS:
            basis = "per pair"
        else:
            continue                    # Per Cwt replacement rows: different basis
        window = ("cur" if iso > cur_lo else
                  ("yr" if yr_lo < iso <= yr_hi else None))
        if not window:
            continue
        b = buckets.setdefault((cls, basis), {"cur": [0, 0.0], "yr": [0, 0.0]})
        b[window][0] += head
        b[window][1] += head * price

    out = []
    for (cls, basis), b in buckets.items():
        if not b["cur"][0]:
            continue
        cur = b["cur"][1] / b["cur"][0]
        yr = (b["yr"][1] / b["yr"][0]) if b["yr"][0] else None
        out.append({"class": cls, "basis": basis, "price": cur,
                    "head": b["cur"][0], "year_ago": yr,
                    "yoy_pct": (100.0 * (cur - yr) / yr) if yr else None})
    return sorted(out, key=lambda r: -r["head"])


def receipts_yoy(conn, weeks=CURRENT_WEEKS):
    """
    Total auction receipts against the same reports a year earlier.

    Uses the reports' OWN receipts_year_ago field rather than our stored
    history, so the comparison is AMS's like-for-like rather than ours, and it
    works even where our archive has a gap. Receipts are a per-report figure,
    so they are read once per (date, slug) rather than summed over line items.
    """
    latest = latest_date(conn)
    if not latest:
        return None
    cutoff = (date.fromisoformat(latest) - timedelta(weeks=weeks)).isoformat()
    rows = conn.cursor().execute(
        f"SELECT DISTINCT report_date, slug_id, receipts, receipts_year_ago "
        f"FROM replacement_sales WHERE report_date > {db.placeholders(1)} "
        f"AND receipts IS NOT NULL", (cutoff,)).fetchall()
    now = sum(int(r[2]) for r in rows if r[2])
    then = sum(int(r[3]) for r in rows if r[3])
    if not then:
        return None
    return {"receipts": now, "year_ago": then, "n_reports": len(rows),
            "pct": 100.0 * (now - then) / then, "weeks": weeks}


# ── Heifer share of feeder receipts ──────────────────────────────────────────
#
# The volume-side counterpart to the retention incentive above. That ratio
# prices the DECISION a producer faces; this measures what they actually did --
# when heifers are kept back to breed, they stop arriving at the feeder auction
# and the heifer share of receipts falls. The two can disagree, which is the
# reason for carrying both: an incentive that nobody acts on is not a rebuild.
#
# Fed by feeder_sex_mix.py, which stores weekly steer/heifer head per report.
# Read its module docstring before changing anything here -- it records why the
# two sources can be spliced and, more usefully, what could NOT be verified.

YTD_CUT = 37                    # ISO week the annual comparison runs through
ROLLING_WEEKS = 52
MIN_YEAR_WEEKS = 30             # below this a year is partial and not comparable

# USDA retired the legacy archive mid-2019 and stood up its replacement in the
# same weeks. Legacy runs normally through week 17 and then falls off a cliff --
# 41k, 27k, 17k, 6k head against a 115k norm -- while MARS only completes its
# panel at week 19. So 2019 takes each half from whichever source was whole at
# the time. Applied as a general preference rather than a special case for 2019,
# it also resolves correctly for every other year, where only one source exists.
LEGACY_LAST_GOOD_YEAR = 2019
LEGACY_LAST_GOOD_WEEK = 17


def _feeder_weeks(conn):
    """{(iso_year, iso_week): {source: [steers, heifers]}}"""
    rows = conn.cursor().execute(
        "SELECT week_start, source, steers, heifers FROM feeder_receipts").fetchall()
    out = {}
    for ws, src, s, h in rows:
        y, w, _ = date.fromisoformat(str(db.iso(ws))).isocalendar()
        d = out.setdefault((y, w), {})
        v = d.setdefault(str(src), [0, 0])
        v[0] += int(s or 0)
        v[1] += int(h or 0)
    return out


def _pick(by_source, year, week):
    """The source to trust for this week, and its [steers, heifers].

    The cutoff is absolute, not a per-week preference. The legacy archive does
    not stop cleanly: it keeps emitting a thinning remnant of stragglers for
    months afterwards, down to a single week of 2020 carrying 420 head against
    MARS's 176,326 for the same week. A rule that merely preferred legacy in
    early weeks would take the 420 and discard the real reading -- which it
    did, tagging 2020 as spliced and quietly dropping a week of the year.

    So legacy is authoritative up to the week it was last whole and is never
    consulted after it, even as a fallback: past that point its absence is
    information, and its presence is noise.
    """
    if (year, week) <= (LEGACY_LAST_GOOD_YEAR, LEGACY_LAST_GOOD_WEEK):
        for src in ("legacy", "mars"):
            if by_source.get(src):
                return src, by_source[src]
    elif by_source.get("mars"):
        return "mars", by_source["mars"]
    return None, None


def heifer_share_annual(conn):
    """
    [{year, share, steers, heifers, src, weeks}] year-to-date through week 37.

    Annual rather than rolling because this is the only basis comparable across
    the 2019 handover: a 52-week window spanning the seam would mix the two
    archives mid-window. Every point covers the same calendar span.
    """
    weeks = _feeder_weeks(conn)
    per_year = {}
    for (y, w), by_source in weeks.items():
        if w > YTD_CUT:
            continue
        src, v = _pick(by_source, y, w)
        if not src:
            continue
        d = per_year.setdefault(y, {"steers": 0, "heifers": 0, "srcs": set(), "weeks": 0})
        d["steers"] += v[0]
        d["heifers"] += v[1]
        d["srcs"].add(src)
        d["weeks"] += 1

    out = []
    for y in sorted(per_year):
        d = per_year[y]
        total = d["steers"] + d["heifers"]
        # A year missing a third of its weeks is not comparable to a whole one.
        # This is what excludes 2010: the legacy archive begins in June of that
        # year, leaving 12 of the 37 weeks.
        if not total or d["weeks"] < MIN_YEAR_WEEKS:
            continue
        out.append({"year": y, "steers": d["steers"], "heifers": d["heifers"],
                    "share": 100.0 * d["heifers"] / total, "weeks": d["weeks"],
                    "src": "spliced" if len(d["srcs"]) > 1 else d["srcs"].pop()})
    return out


def heifer_share_rolling(conn, source="mars"):
    """
    [{week, share, steers, heifers}] on a trailing 52-week window.

    Seasonally neutral, so it puts the turn on its actual date instead of in
    whichever annual bucket the calendar assigns it. Confined to one source for
    the reason given above.

    Head counts are NOT exposed here as a series: a rolling sum steps down
    whenever a report simply misses a week, so it would read reporting gaps as
    market change. That artefact cancels in the share, because the missing week
    leaves the numerator and denominator together.
    """
    weeks = _feeder_weeks(conn)
    have = sorted(k for k, v in weeks.items() if v.get(source))
    if len(have) <= ROLLING_WEEKS:
        return []

    vals = [weeks[k][source] for k in have]
    # Reports publish with a lag, so the newest week is routinely a partial
    # count that looks like a collapse in volume rather than a missing one.
    # Drop from the end while a week carries under 60% of the preceding eight.
    while len(have) > ROLLING_WEEKS + 1:
        tail = sum(vals[-1])
        ref = median(sum(v) for v in vals[-9:-1])
        if not ref or tail >= 0.60 * ref:
            break
        have.pop()
        vals.pop()

    out = []
    for i in range(ROLLING_WEEKS - 1, len(have)):
        window = vals[i - ROLLING_WEEKS + 1:i + 1]
        s = sum(v[0] for v in window)
        h = sum(v[1] for v in window)
        if not (s + h):
            continue
        y, w = have[i]
        out.append({"week": date.fromisocalendar(y, w, 1).isoformat(),
                    "share": 100.0 * h / (s + h), "steers": s, "heifers": h})
    return out


def heifer_share_summary(conn):
    """Headline figures for the page: where this cycle sits against the last."""
    ann = heifer_share_annual(conn)
    if len(ann) < 3:
        return None
    cur = ann[-1]
    lo = min(ann, key=lambda r: r["share"])
    hi = max(ann, key=lambda r: r["share"])
    earlier = [r for r in ann if r["year"] < cur["year"] and r["share"] <= cur["share"]]
    prior = {r["year"]: r for r in ann}
    y24 = prior.get(cur["year"] - 2)
    return {
        "current": cur, "low": lo, "high": hi, "annual": ann,
        "since": max(earlier, key=lambda r: r["year"])["year"] if earlier else None,
        "gap_to_low": cur["share"] - lo["share"],
        "fall_from_high": cur["share"] - hi["share"],
        "heifer_chg": cur["heifers"] - y24["heifers"] if y24 else None,
        "steer_chg": cur["steers"] - y24["steers"] if y24 else None,
        "chg_base_year": y24["year"] if y24 else None,
    }


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    conn = db.get_conn()
    d = decompose(conn)
    print("=== retention incentive ===")
    if d:
        print(f"   through {d['latest']}, trailing {d['weeks']} weeks "
              f"({d['n_current']} sale dates, {d['bred_head']:,} bred head)")
        print(f"   bred    ${d['bred']:,.0f}/hd")
        print(f"   salvage ${d['salvage']:,.0f}/hd")
        print(f"   premium ${d['premium']:,.0f}   ratio {d['ratio']:.2f}")
        print(f"   baseline {d['base_ratio']:.2f} ({d['base_years'][0]}-"
              f"{d['base_years'][1]}, n={d['n_base']})  -> {d['ratio_vs_base']:+.2f}")
        if "driver" in d:
            print(f"   vs {d['prior_year']}: bred {d['bred_yoy_pct']:+.1f}%, "
                  f"salvage {d['salvage_yoy_pct']:+.1f}%  -> {d['driver']}")
    print("\n=== annual ===")
    for y, ratio, bred, salv, n in annual_ratio(conn):
        print(f"   {y}  ratio {ratio:.2f}  bred ${bred:,.0f}  "
              f"salvage ${salv:,.0f}  (n={n})")
    print("\n=== class prices, trailing 4 weeks ===")
    for r in class_prices(conn):
        yo = f"{r['yoy_pct']:+.1f}%" if r["yoy_pct"] is not None else "n/a"
        print(f"   {r['class']:<16} {r['basis']:<9} ${r['price']:>8,.0f}  "
              f"{r['head']:>6,} hd   YoY {yo}")
    rc = receipts_yoy(conn)
    if rc:
        print(f"\n=== receipts, trailing {rc['weeks']} weeks ===")
        print(f"   {rc['receipts']:,} head across {rc['n_reports']} reports, "
              f"vs {rc['year_ago']:,} a year ago -> {rc['pct']:+.1f}%")
    conn.close()
