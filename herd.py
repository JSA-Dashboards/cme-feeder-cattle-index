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
