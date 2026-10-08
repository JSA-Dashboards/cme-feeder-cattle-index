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

# A BARN IS A SET OF SLUGS, NOT ONE SLUG, and this page was wrong about that
# until 2026-10-07. Five of the twelve markets report their bred females under a
# "Replacement Special" slug carrying NO slaughter side at all -- Salina 1893,
# Billings 2257, Tina 3648, the Joplin special 1798, Palmyra 1816 -- between them
# 179,481 of 354,109 bred head, 50.7%. retention_incentive() keyed on report_date
# alone, so that bred head was divided by whatever OTHER barn happened to report
# a cull price the same day. The page promises "sell her bred to a neighbour, or
# ship her to the packer": one animal, one market. Pairing by barn restores it.
BARN_OF_SLUG = {
    1797: "Joplin, MO",         1798: "Joplin, MO",
    1651: "West Plains, MO",
    1788: "Springfield, MO",
    1816: "Palmyra, MO",        1789: "Palmyra, MO",
    3648: "Tina, MO",           3635: "Tina, MO",
    1823: "Oklahoma City, OK",
    1824: "Woodward, OK",
    1825: "El Reno, OK",
    1843: "Ada, OK",
    2257: "Billings, MT",       1774: "Billings, MT",   1776: "Billings, MT",
    1893: "Salina, KS",
}

# A REPLACEMENT SPECIAL IS NOT HELD ON SALE DAY, so "same barn, same date" would
# throw the special away rather than fix it. Billings, Tina and the Joplin
# special pair on the exact date ZERO times out of 90, and every one of them
# within a week (median gap 2, 3 and 1 days); Palmyra is the only one that lands
# on the day, 93% of the time. A cull cow's salvage value does not move
# materially in two days -- pairing her against a different STATE does. So the
# rule is the nearest salvage at the SAME barn, with the gap carried on the row
# rather than hidden, the convention mx_prices.compare() already uses for the
# border quote. 96% of paired observations still come out same-day.
MAX_PAIR_GAP_DAYS = 7

# Salina reports bred females and no slaughter cows, and its companion slug 1892
# carries neither, so there is nothing at that market to pair against. It drops
# out of the ratio entirely; the page names it rather than letting a reader
# assume the panel is still twelve markets.
UNPAIRABLE_BARNS = ("Salina, KS",)

# THE CACHE SERVES SHAPE, NOT FRESHNESS. app.py's load_all() is
# @st.cache_data, which keys on the DECORATED function's own code and its
# arguments and never on the modules it calls -- so every change in this file
# is invisible to it and the page goes on serving the previous dict with no
# error and no stale marker. Bump this whenever retention_incentive(),
# decompose() or annual_ratio() changes the SHAPE of what it returns, and pass
# it into the cached call so it lands in the key. The same trap is recorded for
# leverage.SCHEMA, am_cutout.SCHEMA, mx_prices.SCHEMA and wasde.SCHEMA.
#
# 2  same-barn pairing: rows gained `barn` and `gap_days`, decompose() gained
#    n_dates/n_barns/n_base_dates/unpairable, annual_ratio() gained months and
#    barns and now drops years under MIN_ANNUAL_MONTHS.
SCHEMA = 2


def _rows(conn, since_iso=None):
    where = f"WHERE report_date >= {db.placeholders(1)}" if since_iso else ""
    args = (since_iso,) if since_iso else ()
    return conn.cursor().execute(
        f"SELECT report_date, slug_id, commodity, class_desc, price_unit, head_count, "
        f"avg_weight, avg_price, age, receipts, receipts_year_ago "
        f"FROM replacement_sales {where}", args).fetchall()


def latest_date(conn):
    r = conn.cursor().execute(
        "SELECT MAX(report_date) FROM replacement_sales").fetchone()
    return str(db.iso(r[0])) if r and r[0] else None


def retention_incentive(conn, since_iso=None):
    """
    One row per BARN per sale date: bred value per head, that same barn's
    slaughter-cow salvage per head, and the ratio between them.

    Salvage is converted to a per-head basis (Per Cwt x weight / 100) because
    bred females trade per head and slaughter cows per hundredweight. Comparing
    them unconverted is the single easiest way to produce nonsense here.

    BOTH SIDES COME FROM THE SAME MARKET -- see BARN_OF_SLUG, which this keyed
    past until 2026-10-07, and MAX_PAIR_GAP_DAYS for why the pairing is nearest
    rather than exact. Each row carries `barn` and `gap_days` so a caller can
    see which market it came from and how far the two sides sit apart.
    """
    bred, salv = {}, {}
    for (rd, slug, commodity, cls, unit, head, wt, price,
         _age, _r, _ry) in _rows(conn, since_iso):
        barn = BARN_OF_SLUG.get(slug)
        if not (barn and price and head):
            continue
        day = date.fromisoformat(str(db.iso(rd)))
        if (commodity == "Replacement Cattle" and cls in BRED_CLASSES
                and unit in PER_HEAD_UNITS):
            d = bred.setdefault((barn, day), [0, 0.0])
            d[0] += head
            d[1] += head * price
        elif (commodity == "Slaughter Cattle" and cls == "Cows"
              and unit == "Per Cwt" and wt):
            d = salv.setdefault((barn, day), [0, 0.0])
            d[0] += head
            d[1] += head * price * wt / 100.0

    salv_days = {}
    for (barn, day) in salv:
        salv_days.setdefault(barn, []).append(day)
    for days in salv_days.values():
        days.sort()

    out = []
    for (barn, day), (bh, bd) in bred.items():
        days = salv_days.get(barn)
        if not days:
            continue
        # Ties break to the EARLIER date: a cull price printed before the bred
        # sale was information the buyer had; one printed after it was not.
        near = min(days, key=lambda x: (abs((x - day).days), x))
        gap = abs((near - day).days)
        if gap > MAX_PAIR_GAP_DAYS:
            continue
        sh, sd = salv[(barn, near)]
        b, s = bd / bh, sd / sh
        out.append({"date": day.isoformat(), "barn": barn, "gap_days": gap,
                    "bred": b, "salvage": s, "premium": b - s, "ratio": b / s,
                    "bred_head": bh, "salvage_head": sh})
    out.sort(key=lambda r: (r["date"], r["barn"]))
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
    # An OBSERVATION is one barn on one sale date, so it is no longer the same
    # thing as a sale date -- several barns sell on a Tuesday. Both counts are
    # returned because the caption quotes dates and the median is over
    # observations, and conflating them overstated the sample before.
    out = {
        "latest": latest, "weeks": CURRENT_WEEKS, "n_current": len(cur),
        "n_dates": len({r["date"] for r in cur}),
        "n_barns": len({r["barn"] for r in cur}),
        "bred": med(cur, "bred"), "salvage": med(cur, "salvage"),
        "premium": med(cur, "premium"), "ratio": med(cur, "ratio"),
        "base_ratio": med(base, "ratio"), "base_years": BASELINE_YEARS,
        "n_base": len(base), "n_base_dates": len({r["date"] for r in base}),
        "bred_head": sum(r["bred_head"] for r in cur),
        "unpairable": UNPAIRABLE_BARNS,
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


# A year needs this many distinct months before it is drawn as a bar. The MARS
# floor leaves 2018 with October-December at one barn -- 13 observations, which
# median to 1.61 and would print as the highest year on the chart. A three-month
# window is not a year, and a bar labelled 2018 says it is.
MIN_ANNUAL_MONTHS = 6


def annual_ratio(conn):
    """
    [(year, median ratio, median bred, median salvage, n obs, n months, n barns)].

    The last three are coverage, and the chart needs them: n obs counts barn
    sale-dates rather than sale dates, and a year short of twelve months is
    labelled by its span rather than passed off as a full year. The ratio is
    seasonal, so a part year is not comparable to a whole one -- the same reason
    YTD_CUT exists for the heifer-share chart further down this file.

    Years under MIN_ANNUAL_MONTHS are dropped rather than drawn small.
    """
    inc = retention_incentive(conn)
    by = {}
    for r in inc:
        by.setdefault(r["date"][:4], []).append(r)
    out = []
    for y, v in sorted(by.items()):
        months = {x["date"][:7] for x in v}
        if len(months) < MIN_ANNUAL_MONTHS:
            continue
        out.append((y, median(x["ratio"] for x in v), median(x["bred"] for x in v),
                    median(x["salvage"] for x in v), len(v), len(months),
                    len({x["barn"] for x in v})))
    return out


def annual_span(conn, year):
    """
    ("Jan", "Oct") for a part year, or None when it covers all twelve months.

    The chart labels an incomplete year with its span instead of its number, so
    a ten-month bar standing beside twelve-month bars says so on its own line.
    """
    months = sorted({r["date"][5:7] for r in retention_incentive(conn)
                     if r["date"][:4] == str(year)})
    if len(months) >= 12:
        return None
    if not months:
        return None
    name = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
            "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    return (name[int(months[0]) - 1], name[int(months[-1]) - 1])


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
    for (rd, _slug, commodity, cls, unit, head, _wt, price, _age, _r, _ry) in _rows(conn):
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

# ...but a year between these two bars still earns a place in the CAVEATED
# segment rather than vanishing. 2002-2004 sat there: video carried 27-29 of the
# 37 weeks, so the all-channel rule (every channel or skip the week) put them
# under 30 and they disappeared from a chart that had shown them.
#
# MEASURED, NOT ASSUMED. The worry with a short year is seasonal bias, and it is
# a real worry -- weeks 11-20 average 44.2% against 39.3% for weeks 21-30, a
# 4.9-point swing. But the gaps in those years are SCATTERED, not clustered:
# reweighting each year's weeks to the seasonal norm moves 2002 by -0.28 points,
# 2003 by -0.31 and 2004 by +0.02. Years already in the clean series carry as
# much (2007, +0.20 on 35 weeks).
#
# This used to end "and all of it is far under the +1.62-point video seam the
# recent end already rests on". THERE IS NO VIDEO SEAM; see CHANNEL_LEGACY_THROUGH
# below. The comparison is kept without it because the measured bias stands on
# its own.
#
# 27 is where the evidence runs out rather than a round number: it is 2003, the
# thinnest year measured, and nothing below it has been checked.
MIN_THIN_WEEKS = 27

# Below this many reporting states a year is not comparable either, however many
# weeks it has. The auction archive reaches back to 2000, but coverage builds:
# 12 states in 2000-01, 13, then 16, and 18 only from 2005. A year drawn from
# twelve states is a different survey wearing the same name, and the share it
# yields is not a smaller sample of the national one -- it is a different mix of
# states, which is the whole quantity being measured.
#
# 17 is the floor the existing trusted series already sits on (2013-2017), so
# this admits 2005-2010 and excludes 2000-2004 rather than being tuned to a
# preferred answer.
#
# THE TEST IS STATES, NOT HEAD COUNT, and that distinction caught a real error.
# Judging coverage by head against the 2011-2019 mean flags 2015 as thin at
# 0.88 -- but 2015 has the same 17 states as the years either side of it and is
# low because fewer cattle were sold that year, which is the signal, not a gap
# in it. Head count conflates market volume with survey coverage. States do not.
MIN_PANEL_STATES = 17

# USDA retired the legacy archive mid-2019 and stood up its replacement in the
# same weeks. Legacy runs normally through week 17 and then falls off a cliff --
# 41k, 27k, 17k, 6k head against a 115k norm -- while MARS only completes its
# panel at week 19. So 2019 takes each half from whichever source was whole at
# the time. Applied as a general preference rather than a special case for 2019,
# it also resolves correctly for every other year, where only one source exists.
LEGACY_LAST_GOOD_YEAR = 2019
LEGACY_LAST_GOOD_WEEK = 17


# ALL THREE CHANNELS. The sale barn is roughly 60% of the feeder trade; direct
# (feedlot-to-feedlot, country trade) and video/internet auctions are the rest,
# and a national heifer share has to carry them.
#
# IT WAS AUCTION-ONLY UNTIL 2026-09-29, and not by oversight: USDA retired the
# legacy archives that carried direct and video, so those channels stopped in
# 2020/21 while auction ran on. Summing what existed produced three channels
# through 2020 and auction alone after -- a 2-to-4 point step at the seam that
# read exactly like a market move. That shipped for a day and was reverted.
#
# What changed is that the gap got closed at source. MARS serves these reports
# after all, through a per-section endpoint the repo had not used; see
# feeder_sex_mix.ingest_mars_channels and direct_reports.py's docstring. Every
# channel now runs to the present.
#
# IT IS NOT A COSMETIC WIDENING. The auction-vs-all-channel gap is not a
# constant offset -- it was 4.57 points in 2015 and is 2.25 now, because the
# direct channel's heifer share climbed about twelve points over that span while
# auction's moved half a point. Since this page's headline is the DISTANCE
# between today and the 2015 rebuild, that drift lands squarely on it: auction
# alone says +0.64 points, all three say +2.96. Same direction, materially
# different message, and the wider measure is the more national one.
CHANNELS = ("auction", "direct", "video")

# Which archive OWNS a week, per channel. Legacy holds weeks up to and including
# the entry; MARS holds everything after.
#
# One rule per channel because the handovers happened at different times AND
# overlap differently: auction's two sources share 26 weeks, video's share 19,
# and direct's abut exactly with none. Summing an overlapping week double-counts
# it; preferring whichever source has more rows lets a dying remnant win at
# precisely the moment the remnant is largest. An absolute boundary does neither.
CHANNEL_LEGACY_THROUGH = {
    "auction": (2019, 17),   # MARS auction completes its panel at W19
    "direct": (2020, 38),    # MARS direct detail begins 2020-09-21 = W39
    "video": (2020, 19),     # see below -- NOT 18
}

# VIDEO'S BOUNDARY IS 19, NOT 18, AND THAT IS A CORRECTION.
#
# MARS video's first week is 2020-05-04 = ISO 2020W19, so handing that week to
# MARS looked right. It is not: in that week legacy carries 28,687 head and MARS
# only 1,967, because MARS is starting up rather than legacy finishing. The old
# boundary dropped 26,720 head. Nothing published moved -- 2020 is in SKIP_YEARS
# and the rolling series starts after direct's later handover -- but a rule that
# silently discards 93% of a week is wrong whether or not anyone is reading it.
#
# Safe in the other direction too: after that week legacy video never exceeds
# 6,408 head, so giving it W19 cannot let a remnant outvote a real MARS week.
#
# THERE IS NO VIDEO SEAM, and the "+1.62 points" this file used to cite as one
# was a measurement error of mine. It compared legacy 2019 with MARS 2021 -- two
# years apart, with 2020 skipped between them -- and called the difference a
# join. The adjacent-year join is legacy 2019 36.40% against MARS 2020 36.27%:
#
#     -0.13 pt.
#
# What the +1.62 measures is two years of market movement, and it is real:
#   * Within MARS alone, on a common week window, 2020 36.27% -> 2021 37.19% ->
#     2022 37.91%. A steady climb of about +0.9/yr with no archive change in it.
#   * It is WITHIN-location, not mix: decomposing legacy 2019 -> MARS 2021 gives
#     +1.62 within-location and -0.10 from the roster. Nine of eleven comparable
#     auctions rose, and the five carrying 92% of head all rose together --
#     Superior +1.61, Western +1.84, Cattle Country +1.57, Joplin +2.78,
#     Norwood +2.06. A coverage artefact moves a few locations; it does not move
#     every large one by the same amount.
#   * Norwood is the location where the two archives are PROVEN identical
#     (+0.22 pt over 12 paired weeks, see feeder_sex_mix.py) and it rose +2.06 --
#     more than the aggregate.
#   * Every artefact that could be quantified runs the OTHER way, hiding about
#     0.6 pt of real movement rather than manufacturing any: the unmatched
#     locations -0.07, legacy 2019's Northern hole -0.31 to -0.41, week-mix -0.21.
#
# The control channels were never controls, which is why this looked anomalous.
# Direct's tidy -0.12 across the same span is two large offsetting moves
# (44.09 -> 38.75 -> 43.97), not stability. And auction is the one channel
# holding both sources in the same year: 2019 legacy 46.28% vs 2019 MARS 45.84%,
# a cross-source difference of -0.44 pt. Small, and negative.

# 2020 has no honest annual point and is dropped rather than drawn.
# The basis is year-to-date through week 37, which in 2020 ends 09-13 -- but
# MARS direct does not start until week 39, so 2020 direct is legacy-only, and
# legacy direct by then is a decaying remnant: 53,949 head in the week of 08-03
# falling under 9,000 by 09-14. The point would print as a dip that is purely
# the source handover.
SKIP_YEARS = frozenset({2020})


def _feeder_weeks(conn):
    """{(iso_year, iso_week): {channel: [steers, heifers]}}, one source per channel.

    Source selection happens HERE rather than in the callers, because it is a
    property of the data and every caller would otherwise have to remember to
    apply it. The old shape returned {source: ...} and left the choice to
    _pick(); with three channels handing over on three different dates, leaving
    that to callers is how a double-count gets in.
    """
    rows = conn.cursor().execute(
        "SELECT week_start, source, channel, steers, heifers FROM feeder_receipts "
        "WHERE channel IN ('" + "','".join(CHANNELS) + "')").fetchall()
    raw = {}
    for ws, src, ch, s, h in rows:
        y, w, _ = date.fromisoformat(str(db.iso(ws))).isocalendar()
        d = raw.setdefault((y, w, str(ch)), {}).setdefault(str(src), [0, 0])
        d[0] += int(s or 0)
        d[1] += int(h or 0)

    out = {}
    for (y, w, ch), by_source in raw.items():
        want = "legacy" if (y, w) <= CHANNEL_LEGACY_THROUGH[ch] else "mars"
        v = by_source.get(want)
        if v is None:
            # Only the other archive covers this week. Taking it is right at the
            # edges -- legacy before MARS existed, MARS after legacy stopped --
            # and is never a CHOICE between two, which is the case that matters.
            if len(by_source) != 1:
                continue
            v = next(iter(by_source.values()))
        out.setdefault((y, w), {})[ch] = v
    return out


def _feeder_states(conn):
    """{(iso_year, iso_week): {state, ...}} for the auction channel.

    AUCTION ONLY, even though the series is now all-channel. The guard this
    feeds exists because the auction ARCHIVE's panel builds over the early
    years -- 12 states in 2000, 18 by 2005 -- and that is a property of that
    archive, not of the trade. Counting states across all three channels would
    let a year pass on direct and video coverage while the auction panel behind
    60% of its head was still a third missing.

    Kept separate from _feeder_weeks because that function sums head and this
    counts distinct states; combining them means carrying a set through the hot
    loop for the benefit of one guard.
    """
    rows = conn.cursor().execute(
        "SELECT week_start, state FROM feeder_receipts "
        "WHERE channel = 'auction'").fetchall()
    out = {}
    for ws, state in rows:
        y, w, _ = date.fromisoformat(str(db.iso(ws))).isocalendar()
        out.setdefault((y, w), set()).add(state)
    return out


def _annual_rows(conn):
    """Every year's aggregate, UNFILTERED. Both public builders read this.

    Shared so the clean series and the thin one cannot drift apart: they must
    differ only in which years they admit, never in how a year is computed.
    """
    weeks = _feeder_weeks(conn)
    states = _feeder_states(conn)
    per_year = {}
    for (y, w), by_channel in weeks.items():
        if w > YTD_CUT or y in SKIP_YEARS:
            continue
        # EVERY channel, or the week is skipped. A week carrying two of three
        # is not a smaller sample of the national mix, it is a different mix --
        # the channels sit 10 points apart, so dropping one moves the share far
        # more than the missing head would suggest.
        if len(by_channel) != len(CHANNELS):
            continue
        d = per_year.setdefault(y, {"steers": 0, "heifers": 0, "srcs": set(),
                                    "weeks": 0, "states": set()})
        d["states"] |= states.get((y, w), set())
        for ch in CHANNELS:
            d["steers"] += by_channel[ch][0]
            d["heifers"] += by_channel[ch][1]
        d["srcs"].add("legacy" if (y, w) <= CHANNEL_LEGACY_THROUGH["auction"]
                      else "mars")
        d["weeks"] += 1

    out = []
    for y in sorted(per_year):
        d = per_year[y]
        total = d["steers"] + d["heifers"]
        # A year missing a third of its weeks is not comparable to a whole one.
        # This excluded 2010 until the wtd_1 half of the auction archive was
        # loaded (2026-09-28); 2010 had begun in June, leaving 12 of 37 weeks.
        # It now runs whole, and the guard is kept for the partial CURRENT year
        # and for anything else that arrives half-formed. A year failing THIS
        # test is not reported by either builder -- it is incomplete, not merely
        # thinly covered, and there is nothing to caveat.
        if not total or d["weeks"] < MIN_THIN_WEEKS:
            continue
        out.append({"year": y, "steers": d["steers"], "heifers": d["heifers"],
                    "share": 100.0 * d["heifers"] / total, "weeks": d["weeks"],
                    "states": len(d["states"]),
                    "src": "spliced" if len(d["srcs"]) > 1 else d["srcs"].pop()})
    return out


def heifer_share_annual(conn):
    """
    [{year, share, steers, heifers, src, weeks, states}] YTD through week 37.

    Annual rather than rolling because this is the only basis comparable across
    the 2019 handover: a 52-week window spanning the seam would mix the two
    archives mid-window. Every point covers the same calendar span.

    Years below MIN_PANEL_STATES are ABSENT, and every caller gets that for
    free. heifer_share_summary reads this, so the benchmark, the peak and the
    distance between them cannot be set by a year drawn from twelve states.
    """
    return [r for r in _annual_rows(conn)
            if r["states"] >= MIN_PANEL_STATES and r["weeks"] >= MIN_YEAR_WEEKS]


def heifer_share_thin(conn):
    """The years heifer_share_annual excludes for coverage, same shape.

    Separate function rather than a flag on the main series, and that is the
    point rather than an inconvenience. A `thin: True` field is something a
    caller has to notice; today's channel bug was exactly a caller not noticing
    a field it had never been told to check. Asking for these by name cannot be
    done by accident, and every existing caller stays correct without edits.

    Draw them detached from the clean series -- no line joining 2004 to 2005 --
    because the gap is the message. They are the same measurement on a smaller
    and shifting set of states, so their LEVEL is not comparable with the rest
    even though each year is internally sound.
    """
    return [r for r in _annual_rows(conn)
            if r["states"] < MIN_PANEL_STATES or r["weeks"] < MIN_YEAR_WEEKS]


# A full year is 52 or 53 weeks; below this it is not one.
MIN_FULL_YEAR_WEEKS = 48


def receipts_volume_annual(conn):
    """[{year, steers, heifers, weeks, complete, era}] -- whole years, all weeks.

    THREE THINGS DIFFER FROM heifer_share_annual, all of them because this is a
    LEVEL and that one is a RATIO.

    1. The whole year, not year-to-date through week 37. The short window exists
       so the current, partial year stays comparable; for a head count it just
       amputates the autumn run, which is when a third of the year's cattle sell.
       The current year is reported here as incomplete instead.

    2. A week missing a channel is KEPT. The share series drops it, correctly:
       the channels sit ~10 points apart, so two of three is a different mix.
       Volume has no such problem and the rule actively harms it -- video
       auctions are EPISODIC, a silent video week means no sale rather than an
       absent report, and skipping it would discard the auction and direct head
       that really did sell that week.

    3. No year is excluded for thin STATE coverage, but they ARE flagged, and
       the distinction cost a shipped error. A head count from twelve states is
       not a biased number the way a share is -- it is simply a smaller true
       one -- so excluding those years would be wrong. But drawing them on one
       continuous line is also wrong, because a line asserts comparability
       along its length: the auction panel fills from 12 states in 2000 to 18
       by 2005, and the 24% "rise" from 2001 to 2005 is mostly that. `thin`
       marks them so a caller can draw them apart, the way the share chart
       already does.

    2020 is still dropped. Legacy direct decays before MARS starts at week 39, so
    2020 direct lands at 74% of 2019 and 82% of 2021 -- a handover artefact that
    would read as a collapse in country trade.
    """
    weeks = _feeder_weeks(conn)
    states = _feeder_states(conn)
    per = {}
    for (y, w), by_channel in weeks.items():
        if y in SKIP_YEARS:
            continue
        d = per.setdefault(y, {"steers": 0, "heifers": 0, "weeks": set(),
                               "states": set()})
        d["states"] |= states.get((y, w), set())
        for ch, v in by_channel.items():
            d["steers"] += v[0]
            d["heifers"] += v[1]
        d["weeks"].add(w)

    out = []
    for y in sorted(per):
        d = per[y]
        if not (d["steers"] + d["heifers"]):
            continue
        n = len(d["weeks"])
        out.append({"year": y, "steers": d["steers"], "heifers": d["heifers"],
                    "weeks": n, "complete": n >= MIN_FULL_YEAR_WEEKS,
                    "states": len(d["states"]),
                    "thin": len(d["states"]) < MIN_PANEL_STATES,
                    # Head does NOT cross the archive handover cleanly even
                    # though share does -- the 2019->2021 head ratio is 1.16 for
                    # auction and 1.30 for video against 0.985 for direct,
                    # because both rosters widened. Callers draw the eras apart.
                    "era": "mars" if y >= 2021 else "legacy"})
    return out


def heifer_share_rolling(conn):
    """
    [{week, share, steers, heifers}] on a trailing 52-week window.

    Seasonally neutral, so it puts the turn on its actual date instead of in
    whichever annual bucket the calendar assigns it.

    Confined to weeks where all three channels are on MARS, which is why it
    starts later than the annual series rather than at the same date: direct's
    MARS coverage begins 2020-09-21, and a trailing 52-week window needs a full
    year behind it. A window spanning a handover would mix two archives
    mid-window, which is exactly what the annual basis exists to avoid.

    Head counts are NOT exposed here as a series: a rolling sum steps down
    whenever a report simply misses a week, so it would read reporting gaps as
    market change. That artefact cancels in the share, because the missing week
    leaves the numerator and denominator together.
    """
    weeks = _feeder_weeks(conn)
    # All three channels present AND all three past their handover, so no
    # window straddles a seam.
    first = max(CHANNEL_LEGACY_THROUGH[c] for c in CHANNELS)
    have = sorted(k for k, v in weeks.items()
                  if len(v) == len(CHANNELS) and k > first)
    if len(have) <= ROLLING_WEEKS:
        return []

    vals = [[sum(weeks[k][c][i] for c in CHANNELS) for i in (0, 1)] for k in have]
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
