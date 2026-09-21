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
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

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

ALL_BARNS = "All barns"
MODE_FCI = "vs FCI"
# NOT "vs bracket average". What this mode computes is a barn's BASIS minus the
# bracket's BASIS (see `rel` in load_barns), which is not the same thing as its
# price minus the bracket's average price -- the two differ by exactly the
# Timing column, and the old label promised the second while the code did the
# first. The maths is right and stays; the words now match it. See the caption.
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
        # This roster really does churn -- 18 of the barns clearing the floor at
        # 700-749 over three months do not clear it at 900-949 -- so a selected
        # barn can vanish from `options` on an ordinary change of weight class.
        #
        # There is deliberately NO manual reset here. Measured on Streamlit 1.63
        # (2026-09-18) by seeding the key with a barn in no roster and by
        # picking one that drops out: the box falls back to its first option,
        # session_state follows, and nothing raises -- `options` is part of the
        # widget's identity, so changing it makes a new widget that takes its
        # default. An explicit reset to ALL_BARNS was written first and removed
        # after it was shown to change nothing either way, because a guard that
        # cannot be made to fire is the shape of the three checks this project
        # already had to go back and fix. ALL_BARNS must stay FIRST in this list
        # for the fallback to land somewhere sensible.
        labels = [ALL_BARNS] + [_label(r) for r in
                                sorted(qual, key=lambda r: (r["barn"], r["state"]))]
        barn = st.selectbox("Sale barn", labels, key=f"{key_prefix}_barn")

    # Alphabetical in the dropdown for findability, ranked by the active metric
    # in the chart and table. Both orderings are deliberate.
    focus = next((r for r in qual if _label(r) == barn), None)
    metric = "basis" if mode == MODE_FCI else "rel"
    metric_label = "basis vs FCI" if mode == MODE_FCI else "basis vs bracket average"
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
            f"**vs FCI** is each barn's price minus the CME Feeder Cattle Index "
            f"on the days that barn actually sold. {wt}-{wt + 49} lb sits inside "
            f"the index's own 700-899 lb band, so this is close to a straight "
            f"barn premium: the whole bracket averages only "
            f"**{_md(_signed(bracket['basis']))}/cwt** against the index. Take "
            f"the weight class lighter and that figure climbs fast — past "
            f"\\$100/cwt under 500 lb — because it is then measuring the "
            f"**weight ramp** rather than the barn, and **vs bracket average** "
            f"is what takes the ramp back out."
        )
    elif mode == MODE_FCI:
        st.caption(
            f"**vs FCI** is each barn's price minus the CME Feeder Cattle Index "
            f"on the days that barn actually sold. The index is 700-899 lb "
            f"steers and this bracket is not, so the number is mostly the "
            f"**weight ramp**, not the barn: the whole bracket averages "
            f"**{_md(_signed(bracket['basis']))}/cwt** against the index and "
            f"every barn in it inherits that. Barn-to-barn spread is worth only "
            f"\\$20-40, so here it is invisible against the ramp — switch to "
            f"**vs bracket average** to see the barn on its own."
        )
    else:
        st.caption(
            f"**vs bracket average** is each barn's basis minus the whole "
            f"{wt}-{wt + 49} lb bracket's basis, so the weight ramp "
            f"(**{_md(_signed(bracket['basis']))}/cwt** against the index) "
            f"cancels and what is left is the barn. Head-weighted across every "
            f"barn in the window it sums to zero by construction, so the number "
            f"reads straight: positive is better than the rest of this bracket, "
            f"and it is comparable across brackets in a way **vs FCI** is not."
        )

    # AXIS rebuilt from the passed palette because app.py's is a local inside
    # `with tab_index:`. Same shape as the Average Basis by Location chart this
    # one sits beside, so the two read as one page.
    AXIS = dict(gridcolor=C["border"], linecolor=C["border"], showgrid=True,
                tickfont=dict(color=C["muted"], size=11),
                title_font=dict(color=C["muted"], size=11), zeroline=False)
    ranked = sorted(qual, key=lambda r: r[metric], reverse=True)
    # Top ten and bottom ten, deduped so a roster under twenty barns is not
    # listed twice -- the Index tab's leaderboard idiom, keyed on barn+state
    # rather than location alone. Re-sorted ascending so the strongest basis
    # lands at the top of a horizontal chart.
    show = list({_label(r): r for r in ranked[:10] + ranked[-10:]}.values())
    show.sort(key=lambda r: r[metric])
    fig = go.Figure(go.Bar(
        x=[r[metric] for r in show], y=[_label(r) for r in show],
        orientation="h",
        # The selected barn is OUTLINED, not recoloured: the bar colour carries
        # the sign, and repainting it would hide whether the barn is above or
        # below the line it was picked to be read against.
        marker=dict(
            color=[C["pos"] if r[metric] >= 0 else C["neg"] for r in show],
            line=dict(color=[C["text"] for r in show],
                      width=[2 if focus and _label(r) == _label(focus) else 0
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

    # Both basis columns show whichever mode is selected. The toggle drives the
    # tiles, the chart and the sort; seeing the two side by side is how a reader
    # learns what the toggle actually did.
    st.dataframe(pd.DataFrame([
        {"Sale barn": r["barn"], "State": r["state"], "Head": r["head"],
         "Prints": r["prints"], "$/cwt": round(r["price"], 2),
         "Avg wt": round(r["weight"]),
         "Index on sale days": round(r["fci"], 2),
         "vs FCI": round(r["basis"], 2),
         "vs bracket": round(r["rel"], 2),
         "Timing": round(r["timing"], 2),
         "Last sale": _fmt_date(r["last"])}
        for r in ranked]), use_container_width=True, hide_index=True)

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
        f"**vs bracket basis** is taken over all {bracket['barns']} barns "
        f"including the thin ones, so the listed rows do not sum to exactly "
        f"zero. **Prints** is how many separate lots make up each row: 900 head "
        f"in two lots and 900 in twenty are different kinds of evidence, which "
        f"is why the count is still shown even though it no longer gates a row."
    )
