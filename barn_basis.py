"""
Basis for one sale barn at one weight bracket, against the feeder cattle index.

Average Basis by Location on the Index tab cannot answer this. It runs on
mars_sales, which by the time the chart sees it is already collapsed to one
price per (date, location) and carries no weight bracket at all -- so it can say
what a barn's index-qualifying cattle did, and nothing about the 450 lb calves
that barn sold the same morning. This module asks the same question of any
bracket from 400 lb up, off the cash series, and lets the reader take the weight
ramp back out so the barns are comparable to each other.

MEASURED ON THE DAYS THE BARN ACTUALLY SOLD. Each lot is paired with that day's
fci_value, not with the window's average index. The difference is the size of
the signal this page exists to show: Bassett NE, 700-749 lb, the three months to
2026-09-17, sold 510 of its 906 head on 2026-06-24 with the index at its window
high of 385.06. Its head-weighted index is 377.53 against a calendar mean of
353.65, so the window pairing reports +82.47 basis where the paired one reports
+58.59. That $23.87 is not a Bassett premium, it is the date it sold -- and real
barn-to-barn spread is only $20-40, so the artifact would swamp the finding. It
also reorders the board: ranking the 58 qualifying barns by one against the
other moves Lamoni 51 to 26 and Kearney 26 to 12, and changes the top five.
The paired index is on the page as its own tile and its own column, because a
number traded on has to be auditable and because it is the answer to "why do
those two barns show the same price and different basis".

HEAD-WEIGHTED, SUM(avg_price*head_count)/SUM(head_count). Note this is a
deliberate divergence from cash_calves.py next door, which is POUND-weighted --
so the same bracket can read a few cents apart on the two tabs. Measured
2026-09-17 across 616 barn/bracket combinations, the two conventions differ by a
median of $0.09 and a 90th percentile of $0.38; the worst seen was $11.85 at
Dunlap on 400-449, where in-bracket weight varies most. Each tab's caption names
its own basis so the gap is explainable rather than mysterious.

DECOUPLED FROM EACH PAGE'S CSS AND PALETTE ON PURPOSE. cash_calves.py takes one
colour because it draws no chart; this one does, so it takes the host's whole
palette and its watermark helper. Both app.py copies define AXIS inside their
`with tab_index:` block and add_watermark at module scope of a file that is
deliberately NOT shared, so neither can be imported from here -- and the two
palettes really differ (the portal page is #f6f8f7 on #d7e2dc, the standalone
#ffffff on #e6eaee). Hardcoding either would be wrong on the other page, and
this file has to stay byte-identical across both under tests/test_no_drift.py.

Reads calf_sales, ingested by calf_sales.py in the cme-feeder-cattle-index repo,
joined to fci_daily for the index side. Neither table is written here and
nothing on the index path reads this module: calf_sales spans 400-900 lb where
the index is 700-899, and recompute_fci_daily() applies no weight filter of its
own, so a misrouted row would be published as though it qualified. The table
name is written unquoted throughout, in prose as well as SQL, because
tests/test_index_isolation.py bans it wrapped in either kind of quote inside an
index module and that habit is what keeps app.py clean.
"""
import datetime as _dt

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

BRACKETS = [400, 450, 500, 550, 600, 650, 700, 750, 800, 850, 900]
WINDOWS = [90, 180, 365]
WINDOW_LABEL = {90: "last 3 months", 180: "last 6 months", 365: "last year"}

# A HEAD floor, not a print floor, and the same number in every window.
#
# A pen runs roughly 30-80 head, so 100 head is at least two pens and usually
# several sale days: a market read rather than one draft of cattle. A print
# count cannot say that. Crawford NE showed a basis at 650-699 off 31 head
# arriving in 3 separate lots -- a third of one load, and it cleared the old
# three-print floor comfortably. Crawford is otherwise a genuinely strong barn,
# at a premium in 9 of its 11 brackets and +$30 to +$45 on the light end, which
# is the point: the floor is about sample size, not about the barn.
#
# It also reads plainly in a caption. "At least 100 head" needs no gloss, where
# "at least 3 prints" invites "three prints of what". Prints stay in the table
# and in the tiles -- 900 head in two lots and 900 head in twenty are different
# kinds of evidence and the reader should see which they have -- they just do
# not gate the row any more.
#
# One number for all three windows because head already scales with the window;
# the old dict scaled the floor a second time on top of that. Measured
# 2026-09-18 over all 33 bracket/window combinations: the 90-day windows hold
# 769 barn/brackets with a median of 168 head (p25 64, p75 364), of which 503
# clear 100. Thinnest combination is 400-449 lb over 90 days at 29 of 68 barns
# -- still more than the chart's top-ten-and-bottom-ten can show -- and the year
# windows keep 65-79 of 80-81. Net across all 33 it drops 100 rows against the
# old print floor (2,053 against 2,153) while removing every 31-head leader.
MIN_HEAD = 100
# A SEPARATE, MUCH LOWER FLOOR, and it has to be: MIN_HEAD above gates a BARN
# over a whole window, while this gates one barn on one DAY. At 700-749 lb the
# median barn-day is 41 head and 72% are under 100, so reusing MIN_HEAD here
# would throw away about three quarters of the series.
#
# What actually poisons a per-barn line is single animals. The five most
# extreme premiums on record came from barn-days of 1, 7, 5, 7 and 1 head;
# at a floor of 10 the worst goes from -120.75 to -70.26 and 82% of barn-days
# survive, against 28% at a floor of 100.
MIN_PRINT_HEAD = 10
# Fewer sale days than this and a line reads as a scatter rather than a
# series. At 700-749 lb, 74 of 84 barns clear 10 days and 67 clear 20, so
# 12 charts nearly every barn while keeping the genuinely sparse ones --
# Belen has 8 over ten months -- out of a shape they cannot support.
MIN_CHART_DAYS = 12
# A LINE ACROSS A LONGER GAP THAN THIS INVENTS TRADES THAT DID NOT HAPPEN.
# A barn sells weekly: the median spacing between one barn's sale days at a
# bracket is 7 days. Three missed weeks is a barn that stopped, not a barn
# between sales, and joining across it draws a straight line through months
# of nothing. Measured at 700-749 lb: El Reno and Beaver have no gap over 21
# days at all, so their lines are untouched; Billings has one of 105 days,
# which is the case this exists for.
MAX_LINE_GAP_DAYS = 21

# How many barns the chart shows from each end of the ranking before pinned
# barns are added. Named rather than written twice as a literal 10, because the
# two slices have to stay the same number or the chart is lopsided.
CHART_ENDS = 10


def keep_selected(selected, options):
    """
    The selection, narrowed to the barns that still exist in the current
    roster, in the roster's own order.

    Split out of render() and given no Streamlit import on purpose, so the
    churn case is testable without a browser. The roster really does churn --
    18 of the barns clearing the floor at 700-749 over three months do not
    clear it at 900-949 -- so a barn that was picked can be absent from
    `options` after an ordinary change of weight class or window.

    st.multiselect raises StreamlitAPIException when a default is not among the
    options, so unlike the single selectbox this replaced, leaving it to
    Streamlit's own widget-identity fallback is not safe to rely on: that path
    ends in a traceback on the page rather than a reset box. Filtering here
    makes the outcome the same in every Streamlit version -- the vanished barn
    is dropped, the ones that survived stay picked -- and
    tests/test_barn_basis_pins.py feeds it a vanished barn to prove it fires.
    """
    keep = set(selected or ())
    return [o for o in options if o in keep]


def pinned_rows(qual, selected, metric):
    """
    The rows the chart draws: the top and bottom `CHART_ENDS` by `metric`, plus
    every selected barn regardless of where it ranked.

    Deduped on the barn label so a roster under twenty barns is not listed
    twice, and so a pinned barn already in one of the ends is not doubled.
    Returned ascending by metric, because the strongest basis has to land at
    the top of a horizontal bar chart.
    """
    ranked = sorted(qual, key=lambda r: r[metric], reverse=True)
    ends = ranked[:CHART_ENDS] + ranked[-CHART_ENDS:]
    keep = set(selected or ())
    pins = [r for r in qual if _label(r) in keep]
    show = list({_label(r): r for r in ends + pins}.values())
    show.sort(key=lambda r: r[metric])
    return show
MODE_FCI = "vs FCI"
# NOT "vs bracket average". What this mode computes is a barn's BASIS minus the
# bracket's BASIS (see `rel` in load_barns), which is not the same thing as its
# price minus the bracket's average price -- the two differ by exactly the
# Timing column, and the old label promised the second while the code did the
# first. The maths is right and stays; the words now match it. See the caption.
#
# The rename half-landed the first time: this constant changed and five strings
# in render() did not, so the toggle read "vs bracket basis" while the caption
# defining it, and the chart axis, still read "vs bracket average" -- the page
# told the reader it was doing the thing this comment says it is not. Every
# caption that NAMES the toggle now interpolates these constants rather than
# repeating them, so the control and the prose describing it cannot drift again.
# The chart axis and the table columns are shorter on purpose and do not use
# them; they must merely never say "average".
MODE_BRACKET = "vs bracket basis"

# The standalone page's colours. The portal hands in its own; see the docstring.
PALETTE = {"bg": "#ffffff", "border": "#e6eaee", "muted": "#6b7280",
           "text": "#1f2328", "pos": "#0d7f3d", "neg": "#c00000"}


def _conn():
    import snowflake_db as _db
    return _db, _db.get_conn()


def _since(_db, days):
    return (f"DATEADD(day, -{int(days)}, CURRENT_DATE())" if _db.use_snowflake()
            else f"date('now','-{int(days)} day')")


# THEN-vs-NOW, mirroring cash_calves.py's columns on this tab's own quantity.
#
# NOT more WINDOWS entries, and here the distinction bites harder than it does
# on the cash tab. WINDOWS already offers 180 and 365, but those AVERAGE basis
# across the period: a barn that ran +$12 all spring and -$4 all autumn reads as
# +$4 over the year, a basis it never actually traded at. These columns are the
# same measure at a point in time instead, so "is Carthage wider than it was"
# has an answer.
#
# 12 MONTHS IS THE COMPARISON THAT MEANS MOST HERE. Basis is strongly seasonal,
# so the 365-day column lands in the same part of the calendar and is close to
# like-for-like; the 182-day one lands in the opposite season and reads as a
# seasonal swing as much as a barn's own change. Both are shown because the
# six-month move is still what a trader feels, but the caption says which is
# which rather than leaving the reader to assume they are equivalent.
LOOKBACKS = [(182, "6 mo ago"), (365, "12 mo ago")]
LOOKBACK_BAND_DAYS = 30

# BEFORE THIS DATE fci_daily IS NOT ACCURATE ENOUGH TO SUBTRACT A PRICE FROM.
#
# The direct-trade component was added to the reconstruction on 2026-08-28, and
# CLAUDE.md records the consequence in one line: agreement with CME is to a
# fraction of a cent FROM that date on. Before it, a whole class of sale is
# absent. Measured 2026-10-21 against CME's own published file:
#
#     2026-04-07..05-07   mean |miss| $2.814   max $7.510   <- the 6 mo band
#     2025-10-06..11-05   mean |miss| $1.691   max $4.058   <- the 12 mo band
#     2026-08-28 onward   mean |miss| $0.005   max $0.022
#
# So "vs FCI 6 mo ago" was being measured against an index that was $2.81 out on
# average, sitting in the same row as a live column accurate to half a cent, and
# the caption said they were comparable. They are not, and a basis quoted to the
# cent against a figure that is dollars wrong is worse than no answer.
#
# vs BRACKET SURVIVES, and the asymmetry is the point rather than an oversight:
# rel is a barn's basis minus the whole bracket's, both built from the same
# fci_daily on the same sale days, so the index error very largely cancels. It
# is a comparison between barns; mode A is a comparison against the index
# itself, and that is the one with nothing solid to stand on.
INDEX_TRUSTWORTHY_FROM = _dt.date(2026, 8, 28)


def drop_empty_lookbacks(table, short):
    """
    Remove a lookback column that has no value in any row.

    NEITHER None NOR NaN RENDERS AS BLANK, which is the whole reason this
    exists: an all-absent column reaches a traded page as a stripe of "None",
    and with the index cutoff in force that is every row of both columns.
    blank_missing_lookbacks below carries what was actually measured, and why
    the first two attempts could not tell a blank from a null.

    Two attempts shipped before this one -- None, then NaN -- and both passed
    the whole suite, because nothing in it builds the DataFrame. Only opening
    the page showed it.

    A SPARSE column is kept: one barn missing a band is a fact about that barn,
    the column still carries real readings, and dropping it would hide them.
    Only a column that says nothing at all goes.
    """
    for _centre, label in LOOKBACKS:
        col = "%s %s" % (short, label)
        if col in table.columns and table[col].isna().all():
            table = table.drop(columns=[col])
    return table


def blank_missing_lookbacks(table, short):
    """
    Turn the surviving lookback columns into TEXT, empty where the value is
    missing, and hand back the column_config that keeps them right-aligned.

    A NULL IS NOT A BLANK. Measured in a browser against this Streamlit, one
    missing cell in a column of real numbers: float NaN prints "None", pandas
    pd.NA prints "None", NumberColumn(format="%.2f") prints "None", and a
    Styler carrying na_rep="" prints "None". Only a string column holding ""
    renders empty, so the column has to stop being numeric for a gap to read
    as a gap.

    The two earlier attempts -- None, then NaN -- were each checked by reading
    pyarrow's to_pylist(), which prints None for ANY null and so could not tell
    them apart, or tell either from a blank. That check could not fail. Only
    the rendered page could, and did.

    THE COST IS THE SORT: the grid sorts text lexically, so clicking one of
    these headers puts "-18.53" after "5.46". Taken deliberately. The caption
    under this table promises "Blank means the barn had no qualifying sales in
    that band"; a column printing "None" breaks that promise on every row with
    no data, where the sort misleads only a reader who sorts by that column.

    Runs AFTER drop_empty_lookbacks, which needs the NaNs to recognise a column
    that says nothing at all.
    """
    cfg = {}
    for _centre, label in LOOKBACKS:
        col = "%s %s" % (short, label)
        if col not in table.columns:
            continue
        table[col] = ["" if pd.isna(v) else "%.2f" % v for v in table[col]]
        try:
            cfg[col] = st.column_config.TextColumn(alignment="right")
        except TypeError:
            # alignment= is newer than this file's floor. Losing it costs
            # alignment; raising here would cost the whole tab.
            cfg[col] = st.column_config.TextColumn()
    return table, cfg


def _cutoff_text():
    """
    "28 August 2026". Built by hand because %-d is glibc-only and raises
    ValueError on Windows, which is where this job actually runs -- the same
    trap _fmt_date below already carries a note about, and which this caption
    fell into anyway.
    """
    d = INDEX_TRUSTWORTHY_FROM
    return "%d %s" % (d.day, d.strftime("%B %Y"))


def band_dates(centre_days, width=LOOKBACK_BAND_DAYS, today=None):
    """
    (oldest, newest) real dates the band covers. Mirrors _band's arithmetic so
    the two cannot drift: _band builds the SQL, this answers questions about it.
    `today` is injectable so the cutoff below is testable without waiting a year.
    """
    today = today or _dt.date.today()
    half = width // 2
    return (today - _dt.timedelta(days=int(centre_days) + half),
            today - _dt.timedelta(days=int(centre_days) - half))


def index_is_trustworthy(centre_days, width=LOOKBACK_BAND_DAYS, today=None):
    """
    May a basis measured AGAINST THE INDEX be quoted for this band?

    Requires the WHOLE band to sit on or after INDEX_TRUSTWORTHY_FROM, not just
    its newest day. A band straddling the cutoff is part good and part dollars
    out, and there is no way to say which part a given barn's sale fell in --
    so the honest answer for a straddling band is the same as for a wholly bad
    one: nothing.
    """
    oldest, _newest = band_dates(centre_days, width, today)
    return oldest >= INDEX_TRUSTWORTHY_FROM


def _band(_db, centre_days, width=LOOKBACK_BAND_DAYS):
    """
    "report_date within `width` days of `centre_days` ago", both backends.
    Centred, not trailing: a trailing month ending 182 days back is really 6.5
    months ago, which would make every column quietly older than its label.
    """
    older, newer = int(centre_days) + width // 2, int(centre_days) - width // 2
    return f"cs.report_date BETWEEN {_since(_db, older)} AND {_since(_db, newer)}"


def _fmt_date(iso):
    """ISO to 'Sep 14'. %-d is glibc-only and raises on Windows."""
    from datetime import date
    try:
        return date.fromisoformat(str(iso)[:10]).strftime("%b %d")
    except Exception:
        return str(iso)


def _signed(v):
    """
    '+$58.59' / '-$18.46'. Basis always carries its sign -- an unsigned basis is
    ambiguous in a way an unsigned price is not. The minus goes ahead of the
    dollar sign rather than after it, matching fmt_price in app.py.
    """
    return f"+${v:,.2f}" if v >= 0 else f"-${abs(v):,.2f}"


def _md(text):
    """
    Dollar signs escaped for st.caption. Streamlit's markdown reads a PAIR of
    them as LaTeX and eats everything between, so a caption naming two prices
    loses the sentence between them and raises nothing. cash_calves.py writes
    \\$ inline for the same reason; a helper is cheaper here because these
    captions carry several figures each.
    """
    return text.replace("$", "\\$")


def _label(r):
    """
    'Carthage MO'. Checked 2026-09-17: no barn name appears under two states in
    the 49,203 rows, so this is unique -- but the rows are still grouped by
    location AND state, the way cash_calves.py does it, so a new barn sharing a
    name would split into two rows rather than silently blending two markets.
    """
    return f"{r['barn']} {r['state']}"


@st.cache_data(ttl=3600, show_spinner=False)
def load_barns(weight_low: int, days: int = 90):
    """
    (barn rows, bracket summary) for one weight bracket over one window, every
    barn, with no print floor applied.

    Returns ([], None) rather than raising if the table is missing: a cash feed
    that is down should leave a quiet page, not a broken one.

    ONE query serves the whole tab. The print floor and the barn choice are
    applied in render() over these rows, so changing either costs no query, and
    no barn name is ever interpolated into SQL -- cash_calves.py interpolates a
    state with bare quotes and gets away with it only because no value in the
    table contains an apostrophe today.
    """
    try:
        _db, conn = _conn()
    except Exception as e:
        # Same contract as the query handler below: a backend that will not open
        # is a failure, not an empty bracket. This path is the likelier of the
        # two in production -- an expired Snowflake session or a missing key
        # reaches here, never the SQL -- so returning a quiet empty result was
        # how the whole tab would have gone dark without saying a word.
        return [], {"error": f"{type(e).__name__}: {e}"}
    try:
        # A PLAIN INNER JOIN, and it must stay one. fci_daily carries a row for
        # every calendar day (weekends hold Friday's value forward) and
        # calf_sales lands only Mon-Fri, so this covers 49,203 of 49,203 rows
        # and 2,536,663 of 2,536,663 head -- verified 2026-09-17 by counting the
        # misses, which are zero. No as-of lookup, no Saturday-to-Monday
        # mapping, no COALESCE. A LEFT JOIN would add no coverage at all; it
        # would only manufacture a null-index row with a null basis on the one
        # day that legitimately has none. In Snowflake fci_daily is CRITICAL in
        # snowflake/02_migrate_data.py and calf_sales is OPTIONAL, pushed about
        # eight minutes later, so between those two pushes the newest index date
        # has no calf rows yet. Omitting that day is correct; a null basis
        # reading as zero on a traded page is not.
        rows = conn.cursor().execute(
            "SELECT cs.location, cs.state, SUM(cs.head_count), COUNT(*), "
            "       SUM(cs.avg_price*cs.head_count)"
            "         / NULLIF(SUM(cs.head_count),0), "
            "       SUM(f.fci_value*cs.head_count)"
            "         / NULLIF(SUM(cs.head_count),0), "
            "       SUM(cs.head_count*cs.avg_weight)"
            "         / NULLIF(SUM(cs.head_count),0), "
            "       MAX(cs.report_date) "
            "FROM calf_sales cs "
            "JOIN fci_daily f ON f.report_date = cs.report_date "
            f"WHERE cs.weight_low = {int(weight_low)} "
            f"  AND cs.report_date >= {_since(_db, days)} "
            "GROUP BY cs.location, cs.state "
            "ORDER BY SUM(cs.head_count) DESC").fetchall()
        # Coerced field by field: Snowflake's connector hands back Decimal and a
        # real datetime.date where SQLite hands back float and ISO text, and the
        # positional unpack sidesteps Snowflake upper-casing every column name.
        out = [{"barn": str(a), "state": str(b), "head": int(c or 0),
                "prints": int(d), "price": float(e), "fci": float(f),
                "weight": float(g), "last": str(_db.iso(h))}
               for a, b, c, d, e, f, g, h in rows if e and f]
        if not out:
            return [], None
        # Re-aggregating the barn rows gives the bracket exactly, not
        # approximately: SUM(head_i * price_i)/SUM(head_i) with price_i itself
        # SUM(p*h)/SUM(h) collapses back to the global SUM(p*h)/SUM(h). So the
        # bracket needs no second query and no second cache entry to keep in
        # step. It deliberately includes the thin barns the display floor drops
        # -- it is the bracket's market, not the leaderboard's.
        head = sum(r["head"] for r in out)
        bracket = {
            "head": head,
            "barns": len(out),
            "price": sum(r["head"] * r["price"] for r in out) / head,
            "fci": sum(r["head"] * r["fci"] for r in out) / head,
            "weight": sum(r["head"] * r["weight"] for r in out) / head,
            "last": max(r["last"] for r in out),
        }
        bracket["basis"] = bracket["price"] - bracket["fci"]
        # Derived here, inside the cached call, on purpose: computing them in
        # render() would mutate rows that Streamlit hands back across reruns.
        for r in out:
            r["basis"] = r["price"] - r["fci"]              # mode A
            r["rel"] = r["basis"] - bracket["basis"]        # mode B
            r["timing"] = r["fci"] - bracket["fci"]
        return out, bracket
    except Exception as e:
        # Hand the reason back rather than an empty result. Returning ([], None)
        # here made a broken query look exactly like a quiet week: the tab drew
        # its "no sales matched" notice and nobody could tell a dead backend from
        # a thin bracket. A wrong table name, an expired Snowflake session and a
        # genuinely empty window all rendered the same sentence. render() splits
        # the two now, so the empty state means empty and an error says so.
        return [], {"error": f"{type(e).__name__}: {e}"}
    finally:
        try:
            conn.close()
        except Exception:
            pass


@st.cache_data(ttl=3600, show_spinner=False)
def load_basis_lookbacks(weight_low: int):
    """
    {(barn, state): {centre_days: {"basis": x, "rel": y}}} -- each barn's basis
    at each LOOKBACKS offset, both modes, over a centred band.

    THE SAME JOIN AND THE SAME WEIGHTING AS load_barns, or the columns are not
    comparable with the ones beside them: an inner join to fci_daily so every
    sale is paired with the index on its OWN sale day, and HEAD-weighted, not
    pound-weighted. (cash_calves.py is pound-weighted for its own good reasons;
    copying that formula here would make a barn's history disagree with its
    present by a few cents for no visible reason.)

    The bracket basis for each band is re-aggregated from the barn rows, exactly
    as load_barns does for the live window -- SUM(head*price)/SUM(head) with
    price itself SUM(p*h)/SUM(h) collapses back to the global figure -- so "rel"
    is measured against the bracket AS IT WAS THEN, not against today's. A barn
    compared to the current bracket would read as having moved when the whole
    market did.

    A BARN ABSENT FROM A BAND IS ABSENT, never zero: zero basis is a real and
    unremarkable reading, so a filled-in zero here is indistinguishable from a
    barn that traded exactly at the index. Empty dict on any failure, same quiet
    degradation as everywhere else on this tab.
    """
    out = {}
    try:
        _db, conn = _conn()
    except Exception:
        return out
    try:
        for centre, _label in LOOKBACKS:
            rows = conn.cursor().execute(
                "SELECT cs.location, cs.state, SUM(cs.head_count), "
                "       SUM(cs.avg_price*cs.head_count)"
                "         / NULLIF(SUM(cs.head_count),0), "
                "       SUM(f.fci_value*cs.head_count)"
                "         / NULLIF(SUM(cs.head_count),0) "
                "FROM calf_sales cs "
                "JOIN fci_daily f ON f.report_date = cs.report_date "
                f"WHERE cs.weight_low = {int(weight_low)} "
                f"  AND {_band(_db, centre)} "
                "GROUP BY cs.location, cs.state").fetchall()
            band = [{"barn": str(a), "state": str(b), "head": int(c or 0),
                     "basis": float(d) - float(e)}
                    for a, b, c, d, e in rows if d and e and c]
            if not band:
                continue
            head = sum(r["head"] for r in band)
            bracket_basis = sum(r["head"] * r["basis"] for r in band) / head
            # OMITTED, not zeroed and not quietly wrong: a band that reaches
            # back past INDEX_TRUSTWORTHY_FROM has no index worth subtracting
            # a price from. render() renders a missing key as blank.
            trust = index_is_trustworthy(centre)
            for r in band:
                entry = {"rel": r["basis"] - bracket_basis}
                if trust:
                    entry["basis"] = r["basis"]
                out.setdefault((r["barn"], r["state"]), {})[centre] = entry
    except Exception:
        return out
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return out


@st.cache_data(ttl=3600, show_spinner=False)
def load_barn_history(barn: str, state: str, weight_low: int):
    """
    [{date, price, bracket, head}] per sale day for ONE barn, the whole history
    rather than the selected window, with the bracket's price on each of those
    same days beside it.

    AGAINST THE BRACKET, NOT AGAINST THE INDEX, and that is a data constraint
    rather than a preference. INDEX_TRUSTWORTHY_FROM is 2026-08-28; calf_sales
    begins 2025-09-01, so a basis-vs-FCI line here would have to throw away
    thirteen of its fourteen months -- 39 sale days survive the cutoff in all,
    and the best-covered barn has 13 of them. That is not a line. The bracket
    comparison has no such problem: it prices a barn against its own bracket on
    the same sale days, so the index error very largely cancels, which is the
    same reasoning the lookback columns already use to blank themselves in
    vs FCI mode.

    HEAD-WEIGHTED, matching the tiles and the table on this page. cash_calves.py
    is pound-weighted for its own good reasons and the two are not
    interchangeable; using its formula here would put a line on the page that
    disagrees with the $/cwt directly above it.

    Days where this barn sold fewer than MIN_PRINT_HEAD are dropped -- see the
    constant for the one-head prints that were otherwise the extremes of it.
    Empty list on any failure, the same quiet degradation as the rest of the
    module.
    """
    out = []
    try:
        _db, conn = _conn()
    except Exception:
        return out
    try:
        cur = conn.cursor()
        bracket = {}
        for d, p in cur.execute(
                "SELECT report_date, "
                "       SUM(avg_price*head_count)/NULLIF(SUM(head_count),0) "
                "FROM calf_sales "
                f"WHERE weight_low = {int(weight_low)} "
                "GROUP BY report_date").fetchall():
            if p:
                bracket[str(_db.iso(d))] = float(p)
        # INTERPOLATED, NOT BOUND, and that is the module's convention rather
        # than laziness: this runs on SQLite locally and Snowflake in
        # production, and the two take different placeholder styles -- "?"
        # against "%s". A bound "?" works in every local test and raises on
        # the deployed backend, where the except below swallows it and the
        # chart simply never appears. It cost two reboots to find.
        _barn = str(barn).replace("'", "''")
        _st = str(state).replace("'", "''")
        rows = cur.execute(
            "SELECT report_date, "
            "       SUM(avg_price*head_count)/NULLIF(SUM(head_count),0), "
            "       SUM(head_count) "
            "FROM calf_sales "
            f"WHERE weight_low = {int(weight_low)} "
            f"  AND location = '{_barn}' AND state = '{_st}' "
            "GROUP BY report_date ORDER BY report_date").fetchall()
        for d, p, h in rows:
            iso = str(_db.iso(d))
            if not p or not h or int(h) < MIN_PRINT_HEAD or iso not in bracket:
                continue
            out.append({"date": iso, "price": float(p),
                        "bracket": bracket[iso], "head": int(h)})
    except Exception:
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return out


def render(tile, muted="#6b7280", colors=None, watermark=None,
           key_prefix="basis"):
    """
    Draw the whole lookup. `tile` is the host page's tile helper, called as
    tile(label, value, sub_html); `muted` is its secondary text colour.

    `colors` overrides PALETTE with the host's own constants and `watermark` is
    its add_watermark, optional -- the chart draws fine without one. Both exist
    because this file must stay byte-identical across two pages that use
    different palettes; see the module docstring.

    key_prefix keeps the widget keys distinct from the Cash Feeder Prices tab,
    which carries its own weight and window boxes in the same session.
    """
    C = dict(PALETTE, **(colors or {}))

    def _sub(t):
        return (f'<div style="color:{muted};font-size:0.72rem;margin-top:5px">'
                f'{t}</div>')

    # Populated out of order -- f1, f3, f4, load, then f2 -- because the barn
    # list depends on the bracket, the window AND the print floor. cash_calves
    # does the same for its state box.
    f1, f2, f3, f4 = st.columns([1, 1.6, 1.1, 1.6])
    with f1:
        # Default is the index's OWN band, by value lookup rather than a magic
        # integer. Mode A reads near zero there (+17.15 over three months), so
        # the reader's first move to a lighter bracket makes the weight ramp
        # visible instead of leaving them to infer it. The 900 label is right as
        # it stands and must not be special-cased to "900+": every weight_low
        # 900 row carries weight_high 950 and the heaviest average weight in the
        # table is 949, so the bracket really is 900-949.
        wt = st.selectbox("Weight class", BRACKETS,
                          index=BRACKETS.index(700),
                          format_func=lambda w: f"{w}-{w + 49} lb",
                          key=f"{key_prefix}_wt")
    with f3:
        days = st.selectbox("Window", WINDOWS, index=0,
                            format_func=lambda d: WINDOW_LABEL[d],
                            key=f"{key_prefix}_days")
    with f4:
        mode = st.radio("Basis measured", [MODE_FCI, MODE_BRACKET], index=0,
                        horizontal=True, key=f"{key_prefix}_mode")

    rows, bracket = load_barns(wt, days)
    qual = [r for r in rows if r["head"] >= MIN_HEAD]

    if bracket and bracket.get("error"):
        # Loud, and it names the exception. This tab reads two tables and a
        # remote warehouse; "something went wrong" would send someone hunting
        # through all three.
        st.error(
            f"Could not load {wt}-{wt + 49} lb basis — the query failed rather "
            f"than returning nothing. This is not an empty window. "
            f"Detail: {bracket['error']}"
        )
        return

    if not bracket:
        st.info(
            f"No {wt}-{wt + 49} lb steer sales matched to an index date in the "
            f"{WINDOW_LABEL[days]}. Basis needs both sides — a sale, and the "
            f"index on the day it happened — so a bracket can look empty here "
            f"while the Cash Feeder Prices tab still shows prices for it. Try "
            f"a wider window."
        )
        return
    if not qual:
        st.info(
            f"{bracket['barns']} barn{'s' if bracket['barns'] != 1 else ''} "
            f"reported {wt}-{wt + 49} lb steers in the {WINDOW_LABEL[days]}, "
            f"but none reached {MIN_HEAD} head. One or two pens is not a "
            f"basis — widen the window and the same barns usually qualify."
        )
        return

    with f2:
        # MULTISELECT, not a single box. The chart's top-ten-and-bottom-ten is a
        # leaderboard, and a reader who follows a particular set of barns is
        # usually following several at once -- the mid-pack ones it hides are
        # exactly the ones they came for. Carthage MO is the largest barn in the
        # 700-749 bracket by head and ranked 36th of 54 over three months on
        # 2026-10-09, so it never appeared; Kingdom City MO ranked 18th and
        # never appeared either. Picking barns here PINS them onto the chart
        # whatever they ranked, in addition to the two ends.
        #
        # Empty means all barns and no pins, which is the old ALL_BARNS sentinel
        # without a sentinel: the constant was removed because an empty
        # multiselect already says it, and a magic first option that means "not
        # a barn" is a value every downstream lookup has to special-case.
        #
        # Alphabetical for findability. The chart and table rank by the active
        # metric instead; both orderings are deliberate.
        labels = [_label(r) for r in
                  sorted(qual, key=lambda r: (r["barn"], r["state"]))]
        # Narrowed BEFORE the widget is drawn -- see keep_selected. A barn that
        # dropped out of the roster would otherwise be a default that is not in
        # the options, which st.multiselect raises on.
        prior = list(st.session_state.get(f"{key_prefix}_barns") or ())
        kept = keep_selected(prior, labels)
        # Reported under the chart, not swallowed. A barn vanishing from the
        # picks on a change of window is the single most confusing thing this
        # control can do, and the reason is nearly always the head floor rather
        # than the barn having no sales -- Unionville MO sold 57 head at 700-749
        # over the three months to 2026-10-09 and 184 over six, so it is absent
        # on one window and present on the next.
        dropped = [p for p in prior if p not in set(labels)]
        picked = st.multiselect(
            "Sale barns", labels, default=kept, key=f"{key_prefix}_barns",
            placeholder="All barns — pick to pin onto the chart")

    # The tiles stay a SINGLE barn's numbers. There is no honest way to tile a
    # price and a basis for four barns at once: head-weighting them together
    # would invent a composite barn nobody trades, and showing the first pick
    # would be a tile whose label and value disagree with the chart beside it.
    # So one pick reads as that barn, and two or more fall back to the bracket,
    # which is the one aggregate on this page that is already defined.
    focus = next((r for r in qual if _label(r) == picked[0]), None)         if len(picked) == 1 else None
    metric = "basis" if mode == MODE_FCI else "rel"
    metric_label = "basis vs FCI" if mode == MODE_FCI else "basis vs bracket"
    metric_title = "Basis vs FCI" if mode == MODE_FCI else "Basis vs bracket"
    subject = focus or bracket

    k = st.columns(4)
    with k[0]:
        st.markdown(tile(_label(focus) if focus else f"{wt}-{wt + 49} lb, all barns",
                         f"${subject['price']:,.2f}",
                         _sub("$/cwt, head-weighted")), unsafe_allow_html=True)
    with k[1]:
        if focus:
            v = focus[metric]
            note = ("against the index on this barn's sale days"
                    if mode == MODE_FCI else f"against the {wt}-{wt + 49} lb bracket")
        elif mode == MODE_FCI:
            v, note = bracket["basis"], "against the index on these sale days"
        else:
            v, note = 0.0, "the bracket is the yardstick"
        st.markdown(tile(metric_title, _signed(v), _sub(note)),
                    unsafe_allow_html=True)
    with k[2]:
        # NOT DECORATION. This is what makes two barns at the same price show
        # different basis, and it is the first thing anyone disputing the number
        # will ask for.
        note = f"bracket averaged ${bracket['fci']:,.2f}"
        if focus and abs(focus["timing"]) >= 1:
            note += f" — timing worth {_signed(focus['timing'])}/cwt"
        st.markdown(tile("Index on those sale days", f"${subject['fci']:,.2f}",
                         _sub(note)), unsafe_allow_html=True)
    with k[3]:
        st.markdown(tile("Head sold", f"{subject['head']:,}",
                         _sub(f"{focus['prints']} prints, last "
                              f"{_fmt_date(focus['last'])}" if focus else
                              f"{bracket['barns']} barns, last "
                              f"{_fmt_date(bracket['last'])}")),
                    unsafe_allow_html=True)

    # The live bracket figure goes into the sentence rather than being described
    # in the abstract -- at 450-499 it reads +$121.88 and the point makes itself.
    #
    # And the ramp warning HAS to be conditional. 700-850 sits inside the
    # index's own 700-899 band, where mode A really is close to a barn premium
    # (+$16.97 over three months), so the flat "this is mostly the weight ramp"
    # sentence would be false on the default view -- the one reading a caption
    # is most likely to trust.
    in_band = 700 <= wt <= 850
    if mode == MODE_FCI and in_band:
        st.caption(
            f"**{MODE_FCI}** is each barn's price minus the CME Feeder Cattle Index "
            f"on the days that barn actually sold. {wt}-{wt + 49} lb sits inside "
            f"the index's own 700-899 lb band, so this is close to a straight "
            f"barn premium: the whole bracket averages only "
            f"**{_md(_signed(bracket['basis']))}/cwt** against the index. Take "
            f"the weight class lighter and that figure climbs fast — past "
            f"\\$100/cwt under 500 lb — because it is then measuring the "
            f"**weight ramp** rather than the barn, and **{MODE_BRACKET}** "
            f"is what takes the ramp back out."
        )
    elif mode == MODE_FCI:
        st.caption(
            f"**{MODE_FCI}** is each barn's price minus the CME Feeder Cattle Index "
            f"on the days that barn actually sold. The index is 700-899 lb "
            f"steers and this bracket is not, so the number is mostly the "
            f"**weight ramp**, not the barn: the whole bracket averages "
            f"**{_md(_signed(bracket['basis']))}/cwt** against the index and "
            f"every barn in it inherits that. Barn-to-barn spread is worth only "
            f"\\$20-40, so here it is invisible against the ramp — switch to "
            f"**{MODE_BRACKET}** to see the barn on its own."
        )
    else:
        st.caption(
            f"**{MODE_BRACKET}** is each barn's basis minus the whole "
            f"{wt}-{wt + 49} lb bracket's basis, so the weight ramp "
            f"(**{_md(_signed(bracket['basis']))}/cwt** against the index) "
            f"cancels and what is left is the barn. Head-weighted across every "
            f"barn in the window it sums to zero by construction, so the number "
            f"reads straight: positive is better than the rest of this bracket, "
            f"and it is comparable across brackets in a way **{MODE_FCI}** is not."
        )

    # AXIS rebuilt from the passed palette because app.py's is a local inside
    # `with tab_index:`. Same shape as the Average Basis by Location chart this
    # one sits beside, so the two read as one page.
    AXIS = dict(gridcolor=C["border"], linecolor=C["border"], showgrid=True,
                tickfont=dict(color=C["muted"], size=11),
                title_font=dict(color=C["muted"], size=11), zeroline=False)
    ranked = sorted(qual, key=lambda r: r[metric], reverse=True)
    # The Index tab's leaderboard idiom -- both ends of the ranking, keyed on
    # barn+state rather than location alone -- plus whatever was pinned above.
    show = pinned_rows(qual, picked, metric)
    fig = go.Figure(go.Bar(
        x=[r[metric] for r in show], y=[_label(r) for r in show],
        orientation="h",
        # The selected barn is OUTLINED, not recoloured: the bar colour carries
        # the sign, and repainting it would hide whether the barn is above or
        # below the line it was picked to be read against.
        marker=dict(
            color=[C["pos"] if r[metric] >= 0 else C["neg"] for r in show],
            line=dict(color=[C["text"] for r in show],
                      width=[2 if _label(r) in set(picked) else 0
                             for r in show])),
        hovertemplate="<b>%{y}</b>: %{x:+.2f} " + metric_label + "<extra></extra>"))
    fig.update_layout(
        paper_bgcolor=C["bg"], plot_bgcolor=C["bg"],
        font=dict(color=C["text"], size=11),
        margin=dict(l=10, r=10, t=10, b=30),
        xaxis=dict(**AXIS, title=f"Avg {metric_label} ($/cwt)"),
        yaxis=dict(**AXIS),
        height=max(320, 22 * len(show) + 80), showlegend=False)
    if watermark:
        watermark(fig, size=0.3, opacity=0.06)
    st.plotly_chart(fig, use_container_width=True)
    if picked:
        st.caption(
            f"The chart shows the top and bottom {CHART_ENDS} barns by "
            f"**{metric_label}**, plus the "
            f"{'barn' if len(picked) == 1 else str(len(picked)) + ' barns'} you "
            f"pinned — **{', '.join(picked)}** — wherever "
            f"{'it' if len(picked) == 1 else 'they'} ranked. Pinned bars are "
            f"outlined. The full ranking is in the table below."
        )
    if dropped:
        st.caption(
            f"**{', '.join(dropped)}** {'was' if len(dropped) == 1 else 'were'} "
            f"dropped from the selection: no {wt}-{wt + 49} lb sales reaching "
            f"{MIN_HEAD} head in the {WINDOW_LABEL[days]}. Widen the window or "
            f"change the weight class and the same barns usually come back."
        )

    # Both basis columns show whichever mode is selected. The toggle drives the
    # tiles, the chart and the sort; seeing the two side by side is how a reader
    # learns what the toggle actually did.
    # The lookback columns carry the ACTIVE metric only. Both basis columns are
    # already shown side by side so the toggle's effect is visible; repeating
    # that for two more offsets would be four extra columns on an eleven-column
    # table, and the one a reader wants is the one they just selected. The
    # header names the mode rather than saying a bare "6 mo ago", because on a
    # table holding two different basis measures an unqualified column is a
    # number whose meaning the reader has to guess.
    back = load_basis_lookbacks(wt)
    short = "vs FCI" if mode == MODE_FCI else "vs bracket"

    def _row(r):
        out = {"Sale barn": r["barn"], "State": r["state"], "Head": r["head"],
               "Prints": r["prints"], "$/cwt": round(r["price"], 2),
               "Avg wt": round(r["weight"]),
               "Index on sale days": round(r["fci"], 2),
               "vs FCI": round(r["basis"], 2),
               "vs bracket": round(r["rel"], 2)}
        prior = back.get((r["barn"], r["state"]), {})
        for centre, label in LOOKBACKS:
            # None, NOT 0.0. Zero basis is a real and ordinary reading here, so a
            # filled-in zero would be indistinguishable from a barn that traded
            # exactly at the index that month.
            # .get(metric), NOT [metric]. A band can carry "rel" and not
            # "basis" -- see index_is_trustworthy -- and a KeyError here would
            # take the whole tab down rather than leaving one column blank.
            #
            # NaN, not None: drop_empty_lookbacks needs .isna() to find a
            # column that says nothing at all, and blank_missing_lookbacks
            # needs it to find the individual gaps. NEITHER renders as blank
            # on its own -- both print the word "None" -- which is what
            # blank_missing_lookbacks exists to deal with.
            p = prior.get(centre) or {}
            v = p.get(metric)
            out[f"{short} {label}"] = round(v, 2) if v is not None else float("nan")
        out["Timing"] = round(r["timing"], 2)
        out["Last sale"] = _fmt_date(r["last"])
        return out

    table = drop_empty_lookbacks(pd.DataFrame([_row(r) for r in ranked]), short)
    table, lookback_cfg = blank_missing_lookbacks(table, short)
    st.dataframe(table, use_container_width=True, hide_index=True,
                 column_config=lookback_cfg)
    st.caption(
        f"**{short} {LOOKBACKS[0][1]}** and **{short} {LOOKBACKS[1][1]}** are "
        f"the same measure at a point in time — that barn's basis over a "
        f"{LOOKBACK_BAND_DAYS}-day band centred on the date, against the bracket "
        f"as it stood *then* — not an average of the months in between. Basis is "
        f"seasonal, so **{LOOKBACKS[1][1]}** is the closer comparison: it lands "
        f"in the same part of the calendar, where **{LOOKBACKS[0][1]}** is the "
        f"opposite season and moves with it. Blank means the barn had no "
        f"qualifying sales in that band."
    )
    stale = [lbl for cen, lbl in LOOKBACKS if not index_is_trustworthy(cen)]
    if stale and mode == MODE_FCI:
        st.caption(_md(
            f"**{' and '.join(stale)} are blank in this mode on purpose.** Those "
            f"bands reach back before {_cutoff_text()} where "
            f"available, and the reconstruction did not carry direct-trade sales "
            f"until then — measured against CME's own file, the index ran "
            f"$2.81 out on average over the six-month band and $1.69 over the "
            f"twelve-month one, against half a cent today. A basis quoted to the "
            f"cent off a figure that is dollars wrong is worse than no answer. "
            f"Switch to **{MODE_BRACKET}** to see those bands: it compares a "
            f"barn against the bracket on the same sale days, so the index error "
            f"very largely cancels."
        ))

    st.caption(
        f"Steers only, muscle grade #1 and #1-2, {wt}-{wt + 49} lb, from the "
        f"same USDA AMS barn reports behind the CME Feeder Cattle Index. "
        f"**Head-weighted**, and every barn's basis is paired with the index on "
        f"its own sale days rather than the window average — a barn that sold "
        f"most of its cattle on one strong day would otherwise read as a "
        f"premium it never earned. **Timing** is exactly that effect, isolated: "
        f"what this barn's sale calendar was worth against the bracket's. Barns "
        f"need at least **{MIN_HEAD} head** in the {WINDOW_LABEL[days]} to be "
        f"listed — a pen runs 30 to 80, so that is two pens and usually several "
        f"sale days rather than one draft of cattle. The bracket basis behind "
        f"**{MODE_BRACKET}** is taken over all {bracket['barns']} barns "
        f"including the thin ones, so the listed rows do not sum to exactly "
        f"zero. **Prints** is how many separate lots make up each row: 900 head "
        f"in two lots and 900 in twenty are different kinds of evidence, which "
        f"is why the count is still shown even though it no longer gates a row."
    )


    # ── One barn, against its bracket, over time ──────────────────────────────
    #
    # The third of the three charts added on 2026-10-22, and the one that could
    # not simply copy the other two.
    #
    # ONE BARN, because that is this tab's unit. With several picked or none,
    # `subject` falls back to the bracket, and charting the bracket against
    # itself says nothing -- so the chart appears exactly when `focus` does.
    # Pooling the picked barns was the other option and is the one the tiles
    # above already refuse, for the reason written there: it would invent a
    # composite barn nobody trades.
    #
    # AGAINST THE BRACKET RATHER THAN THE INDEX, and not to duplicate the Cash
    # Feeder Prices tab -- see load_barn_history for the dates. vs FCI would
    # have 13 points at best.
    #
    # It does NOT follow the vs FCI / vs bracket toggle above. A toggle whose
    # other position draws a near-empty chart is a control that looks broken,
    # and the caption says which comparison this is rather than leaving the
    # reader to assume it tracks the radio.
    if focus is not None:
        _hist = load_barn_history(focus["barn"], focus["state"], wt)
        if len(_hist) >= MIN_CHART_DAYS:
            st.markdown("<div style='height:18px'></div>", unsafe_allow_html=True)
            st.markdown(f'<div class="sec-header">{_label(focus)} vs the '
                        f'{wt}-{wt + 49} lb Bracket</div>',
                        unsafe_allow_html=True)
            _x = [r["date"] for r in _hist]
            _bp = [r["price"] for r in _hist]
            _kp = [r["bracket"] for r in _hist]
            _gp = [a - b for a, b in zip(_bp, _kp)]

            # None at every gap longer than MAX_LINE_GAP_DAYS, with
            # connectgaps=False below, so the line breaks where the barn did
            # rather than running straight through the months it did not sell.
            # The STATS above and below stay on the unbroken lists: a break is
            # a drawing decision, not a missing observation.
            _brk = [0] + [i for i in range(1, len(_x))
                          if (_dt.date.fromisoformat(_x[i])
                              - _dt.date.fromisoformat(_x[i - 1])).days
                          > MAX_LINE_GAP_DAYS]

            def _split(vals):
                out = []
                for i, v in enumerate(vals):
                    if i and i in _brk:
                        out.append(None)
                    out.append(v)
                return out

            def _splitx(vals):
                out = []
                for i, v in enumerate(vals):
                    if i and i in _brk:
                        out.append(v)
                    out.append(v)
                return out

            _xd, _bpd, _kpd, _gpd = (_splitx(_x), _split(_bp),
                                     _split(_kp), _split(_gp))

            hfig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                                 row_heights=[0.72, 0.28], vertical_spacing=0.06)
            hfig.add_trace(go.Scatter(
                x=_xd, y=_bpd, name=_label(focus), mode="lines+markers",
                connectgaps=False,
                line=dict(color=C["neg"], width=1.6),
                marker=dict(size=4),
                hovertemplate="Barn: $%{y:.2f}<extra></extra>"), row=1, col=1)
            hfig.add_trace(go.Scatter(
                x=_xd, y=_kpd, name=f"{wt}-{wt + 49} lb bracket",
                mode="lines", connectgaps=False,
                line=dict(color=C["text"], width=2),
                hovertemplate="Bracket: $%{y:.2f}<extra></extra>"), row=1, col=1)
            hfig.add_trace(go.Scatter(
                x=_xd, y=_gpd, name="premium", mode="lines",
                showlegend=False, connectgaps=False,
                line=dict(color=C["muted"], width=1),
                hovertemplate="Premium: $%{y:+.2f}<extra></extra>"), row=2, col=1)
            hfig.update_yaxes(title="$/cwt", tickprefix="$", row=1, col=1, **AXIS)
            hfig.update_yaxes(title="premium", tickprefix="$", row=2, col=1, **AXIS)
            hfig.update_xaxes(row=1, col=1, **AXIS)
            hfig.update_layout(
                paper_bgcolor=C["bg"], plot_bgcolor=C["bg"],
                font=dict(color=C["text"], size=11),
                hovermode="x unified", showlegend=True,
                legend=dict(orientation="h", yanchor="bottom", y=1.02,
                            xanchor="right", x=1, bgcolor="rgba(0,0,0,0)",
                            font=dict(color=C["muted"], size=10)),
                margin=dict(l=55, r=20, t=15, b=40),
                xaxis2=dict(**AXIS, title="", type="date"),
                xaxis=dict(type="date"),
                height=420)
            if watermark:
                watermark(hfig, size=0.3, opacity=0.06)
            st.plotly_chart(hfig, use_container_width=True)

            _above = sum(1 for g in _gp if g > 0)
            _med = sorted(_gp)[len(_gp) // 2]
            st.caption(_md(
                f"Every sale day {_label(focus)} reported at {wt}-{wt + 49} lb, "
                f"head-weighted like the table above, against the whole "
                f"bracket on those same days. {len(_hist)} sale days, "
                f"{_x[0]} to {_x[-1]}. It came in above the bracket on "
                f"{100 * _above // len(_gp)}% of them; across all of them its "
                f"median premium was ${_med:+.2f}/cwt. "
                f"**Against the bracket, not the index, whichever way the "
                f"toggle above is set** — the reconstruction only carries "
                f"direct trade from {_cutoff_text()}, so a line against the "
                f"index could honestly start no earlier than that and would "
                f"have a handful of points. The bracket comparison prices this "
                f"barn against its peers on the same days, so the index error "
                f"very largely cancels. "
                f"Days under {MIN_PRINT_HEAD} head are left out: a one-head "
                f"print is not a market, and those were the extremes of this "
                f"line."
            ))
        elif _hist:
            st.caption(
                f"{_label(focus)} has only {len(_hist)} qualifying sale "
                f"day(s) at {wt}-{wt + 49} lb, too few to chart a history.")
