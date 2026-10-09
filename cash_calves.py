"""
Cash calf and feeder prices by weight and state, from the AMS barn reports.

Shared by the Backgrounding Crush and the CME Feeder Cattle Index page, which
want the same lookup for different reasons: the backgrounder is deciding what a
calf is worth before bidding, the index reader is asking what the cattle behind
today's print actually brought. Neither needs its own copy, and a third copy is
how two of them drift.

DECOUPLED FROM EACH PAGE'S CSS ON PURPOSE. The two hosts have different tile
helpers -- the crush page's takes (label, value, sub, kind) and wraps the sub in
a .tile-sub div, the index page's takes (label, value, delta) and injects the
third argument as raw HTML, and it has no .tile-sub rule at all. So render()
takes the tile callable and a muted colour, and builds its own sub-line inline.
Passing bare text into the index page's tile would render it unstyled.

POUND-WEIGHTED, the same convention as the index itself: a 300-head draft counts
for more than a four-head pen. The Prints column is deliberate -- a barn showing
a single print is one lot, not a market, and a $/cwt on 4 head is not evidence
of anything.

Reads calf_sales, which is ingested by calf_sales.py in the cme-feeder-cattle-index
repo. That table deliberately spans 400-900 lb, wider than the index's own
700-899 band, because it is a reference series and nothing in
recompute_fci_daily() reads it.
"""
import pandas as pd
import streamlit as st

ALL_STATES = "All states"
CASH_BRACKETS = [400, 450, 500, 550, 600, 650, 700, 750, 800, 850, 900]
WINDOWS = [14, 30, 60, 90, 180, 365]
# 180/365 match barn_basis.py's window lengths, which is the established
# convention here for "6 months" and "a year" as an averaging period. The
# LOOKBACKS centres below are 182/365 and deliberately not the same thing: one
# is how long a period to average, the other is a point to stand at.
WINDOW_LABEL = {14: "last 14 days", 30: "last 30 days", 60: "last 60 days",
                90: "last 90 days", 180: "last 6 months", 365: "last 12 months"}

# Windows long enough that the average stops describing the current market.
#
# These were deliberately absent until 2026-10-07, and the reason has not gone
# away -- it is now LABELLED rather than avoided. The tiles and the table report
# an average over the whole window, and over a year that blends a market that
# moved: on 550-600 lb, monthly averages ran $396.75 in Aug 2026 to $479.99 in
# Apr 2026, and the 12-month figure is $446.61 against a 30-day reading of
# $411.47. A reader who takes $446.61 for today's price is $35 wrong.
#
# So on a long window the headline says what it is. Do not drop that wording to
# tidy the tile: it is the whole reason these entries are safe to offer, and the
# "6 mo ago"/"12 mo ago" COLUMNS remain the way to ask what a price actually was
# at a point in time.
LONG_WINDOWS = (180, 365)

# THEN-vs-NOW, and deliberately NOT more entries in WINDOWS.
#
# Window is an AVERAGING period, so "last 12 months" would be one number blended
# across a year of a trending market -- not a price that was ever true. Measured
# 2026-10-07 on the 550-600 lb bracket: monthly averages ran $396.75 (Aug 2026)
# to $479.99 (Apr 2026), an $83 swing, and the 12-month average comes out
# $446.61 against a 30-day reading of $411.47. Putting $446.61 in a tile
# captioned "what cattle are worth" is the hay-units mistake in CLAUDE.md with a
# different unit: a figure that is arithmetically correct and answers a question
# nobody asked.
#
# So the lookback is a separate COLUMN, not a longer window. Each is a
# pound-weighted average over a 30-day band CENTRED on that date -- the same
# formula as the live reading, so the comparison is like for like, and wide
# enough that a barn selling fortnightly still has prints in it.
LOOKBACKS = [(182, "6 mo ago"), (365, "12 mo ago")]
LOOKBACK_BAND_DAYS = 30


def _conn():
    import snowflake_db as _db
    return _db, _db.get_conn()


def _since(_db, days):
    return (f"DATEADD(day, -{int(days)}, CURRENT_DATE())" if _db.use_snowflake()
            else f"date('now','-{int(days)} day')")


def _band(_db, centre_days, width=LOOKBACK_BAND_DAYS):
    """
    SQL for "report_date within `width` days of `centre_days` ago", on either
    backend. Half the width each side, so the band is centred rather than
    trailing -- a trailing month ending 182 days back is really 6.5 months ago.
    """
    older, newer = int(centre_days) + width // 2, int(centre_days) - width // 2
    return f"report_date BETWEEN {_since(_db, older)} AND {_since(_db, newer)}"


@st.cache_data(ttl=3600, show_spinner=False)
def load_rows(weight_low: int, state: str, days: int = 30):
    """
    (barn rows, summary) for one weight bracket, optionally one state.

    Returns ([], None) rather than raising if the table is missing: a cash feed
    that is down should leave a quiet page, not a broken one.
    """
    try:
        _db, conn = _conn()
    except Exception:
        return [], None
    try:
        where = f"weight_low = {int(weight_low)} AND report_date >= {_since(_db, days)}"
        if state != ALL_STATES:
            where += f" AND state = '{state}'"
        rows = conn.cursor().execute(
            "SELECT location, state, SUM(head_count), "
            "       SUM(head_count*avg_weight*avg_price)"
            "         / NULLIF(SUM(head_count*avg_weight),0), "
            "       SUM(head_count*avg_weight)/NULLIF(SUM(head_count),0), "
            "       MAX(report_date), COUNT(*) "
            f"FROM calf_sales WHERE {where} "
            "GROUP BY location, state ORDER BY SUM(head_count) DESC").fetchall()
        out = [{"barn": str(a), "state": str(b), "head": int(c or 0),
                "price": float(d), "weight": float(e),
                "last": str(_db.iso(f)), "prints": int(g)}
               for a, b, c, d, e, f, g in rows if d and e]
        if not out:
            return [], None
        lb = sum(r["head"] * r["weight"] for r in out)
        head = sum(r["head"] for r in out)
        return out, {
            "head": head,
            "price": sum(r["head"] * r["weight"] * r["price"] for r in out) / lb,
            "weight": lb / head,
            "barns": len(out),
            "last": max(r["last"] for r in out),
        }
    except Exception:
        return [], None
    finally:
        try:
            conn.close()
        except Exception:
            pass


@st.cache_data(ttl=3600, show_spinner=False)
def load_lookbacks(weight_low: int, state: str):
    """
    {(barn, state): {centre_days: price}} -- what each barn's cattle brought at
    each LOOKBACKS offset, pound-weighted over a centred band.

    A BARN WITH NO PRINTS IN A BAND IS ABSENT, not zero and not carried forward.
    Sale barns open, close, change sale days and skip seasons; the honest answer
    for "what did Belen bring 12 months ago" when Belen did not sell is nothing
    at all, and the table renders it blank. Filling it would invent a comparison.

    Same ([], None)-style degradation as load_rows: a missing table leaves the
    columns empty rather than breaking the page.
    """
    out = {}
    try:
        _db, conn = _conn()
    except Exception:
        return out
    try:
        for centre, _label in LOOKBACKS:
            where = (f"weight_low = {int(weight_low)} AND {_band(_db, centre)}")
            if state != ALL_STATES:
                where += f" AND state = '{state}'"
            for loc, stt, price in conn.cursor().execute(
                    "SELECT location, state, "
                    "       SUM(head_count*avg_weight*avg_price)"
                    "         / NULLIF(SUM(head_count*avg_weight),0) "
                    f"FROM calf_sales WHERE {where} "
                    "GROUP BY location, state").fetchall():
                if price:
                    out.setdefault((str(loc), str(stt)), {})[centre] = float(price)
    except Exception:
        return out
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return out


@st.cache_data(ttl=3600, show_spinner=False)
def load_states(days: int = 30):
    """States with prints in the window, deepest first."""
    try:
        _db, conn = _conn()
    except Exception:
        return []
    try:
        return [str(r[0]) for r in conn.cursor().execute(
            f"SELECT state, SUM(head_count) h FROM calf_sales "
            f"WHERE report_date >= {_since(_db, days)} "
            f"GROUP BY state ORDER BY h DESC")]
    except Exception:
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _label(r):
    """
    'Carthage MO'. Matches barn_basis.py's label exactly, so the same barn reads
    the same on both tabs and a reader can carry a name from one to the other.
    """
    return f"{r['barn']} {r['state']}"


def keep_selected(selected, options):
    """
    The picked barns, narrowed to those still in the current roster, in the
    roster's own order.

    A DELIBERATE TWIN of barn_basis.keep_selected rather than an import. These
    two modules are shared into different hosts -- cash_calves also serves the
    Backgrounding Crush page, which has no reason to carry barn_basis -- and a
    cross-import to save three lines would couple them for good. The behaviour
    is identical and tested in both places.

    It has to happen BEFORE the widget draws: st.multiselect raises on a default
    that is not among its options, where a selectbox quietly reset. The roster
    here churns on three controls at once -- weight class, state and window --
    so a pick going stale is ordinary, not an edge case. Picking four Missouri
    barns and then switching the State box to NE is the obvious way in.
    """
    keep = set(selected or ())
    return [o for o in options if o in keep]


def summarise(rows):
    """
    The tile summary over an arbitrary subset of barn rows.

    POUND-WEIGHTED, and it must reproduce load_rows' own summary exactly, not
    approximately -- otherwise filtering to every barn would move the headline
    and the tab would disagree with itself. It collapses back cleanly because
    each row's price is itself SUM(head*wt*p)/SUM(head*wt), so re-weighting by
    head*wt recovers the global ratio. tests/test_cash_barn_filter.py asserts
    that against load_rows rather than taking the algebra on trust.

    Returns None on an empty subset, the same shape load_rows uses for "no
    data", so render() has one empty case to handle rather than two.
    """
    if not rows:
        return None
    lb = sum(r["head"] * r["weight"] for r in rows)
    head = sum(r["head"] for r in rows)
    if not lb or not head:
        return None
    return {
        "head": head,
        "price": sum(r["head"] * r["weight"] * r["price"] for r in rows) / lb,
        "weight": lb / head,
        "barns": len(rows),
        "last": max(r["last"] for r in rows),
    }


def _fmt_date(iso):
    """ISO to 'Sep 14'. %-d is glibc-only and raises on Windows."""
    from datetime import date
    try:
        return date.fromisoformat(str(iso)[:10]).strftime("%b %d")
    except Exception:
        return str(iso)


def render(tile, muted="#6b7280", key_prefix="cash"):
    """
    Draw the whole lookup. `tile` is the host page's tile helper, called as
    tile(label, value, sub_html); `muted` is its secondary text colour.

    key_prefix keeps the widget keys distinct so the same lookup can appear on
    two pages in one session without Streamlit treating them as one widget.
    """
    def _sub(t):
        return (f'<div style="color:{muted};font-size:0.72rem;margin-top:5px">'
                f'{t}</div>')

    # Populated out of order -- f1, f3, f4, load, then f2 -- because the barn
    # list depends on the bracket, the state AND the window, so the rows have to
    # be in hand before that box can be drawn. barn_basis.py does the same.
    # The barn box gets the widest share by a distance. It is the only control
    # here that holds SEVERAL values at once, and Streamlit renders each as a
    # chip inside the box -- at 1.6 against the others, four barn names
    # truncated to "do..." and the control became unreadable at exactly the
    # selection size it exists to serve.
    f1, f2, f3, f4 = st.columns([1, 2.6, 1, 1.2])
    with f1:
        wt = st.selectbox("Weight class", CASH_BRACKETS,
                          index=CASH_BRACKETS.index(600),
                          format_func=lambda w: f"{w}-{w + 49} lb",
                          key=f"{key_prefix}_wt")
    with f4:
        days = st.selectbox("Window", WINDOWS, index=1,
                            format_func=lambda d: WINDOW_LABEL[d],
                            key=f"{key_prefix}_days")
    with f3:
        state = st.selectbox("State", [ALL_STATES] + load_states(days),
                             key=f"{key_prefix}_state")

    rows, summ = load_rows(wt, state, days)
    if not summ:
        st.info(
            f"No {wt}-{wt + 49} lb steer sales reported in "
            f"{'any tracked state' if state == ALL_STATES else state} over the "
            f"{WINDOW_LABEL[days]}. Light calves thin out badly in late summer and "
            f"the heaviest brackets only trade at a few barns — try a wider "
            f"window or a different weight."
        )
        return

    with f2:
        # A FILTER, not a highlight. Unlike the Sale Barn Basis chart next door
        # -- which hid most of the roster behind a top-ten-and-bottom-ten and
        # needed picked barns PINNED back onto it -- this tab has no chart and
        # its table already lists every barn. Nothing is hidden here, so the
        # useful thing is the opposite operation: cut 79 rows down to the few
        # barns someone actually follows, so they can be read against each other
        # without scrolling past everyone else.
        #
        # Empty means every barn, which is the tab exactly as it was.
        options = [_label(r) for r in
                   sorted(rows, key=lambda r: (r["barn"], r["state"]))]
        prior = list(st.session_state.get(f"{key_prefix}_barns") or ())
        kept = keep_selected(prior, options)
        # Named under the table rather than swallowed. Three controls can strand
        # a pick here, and the State box is the loud one: picking Missouri barns
        # and then switching State to NE empties the selection completely, which
        # looks like a bug unless it says why.
        dropped = [b for b in prior if b not in set(options)]
        picked = st.multiselect(
            "Sale barns", options, default=kept, key=f"{key_prefix}_barns",
            placeholder="All barns — pick to compare a few")

    # EVERYTHING below the filter reads the filtered rows, including the tiles
    # and the spread. A headline labelled "4 barns average" showing the whole
    # state's number would be the label-disagrees-with-value failure this
    # project has already had to fix once on the basis toggle.
    summ_all = summ
    if picked:
        rows = [r for r in rows if _label(r) in set(picked)]
        summ = summarise(rows) or summ

    if picked:
        where = picked[0] if len(picked) == 1 else f"{len(picked)} barns"
    else:
        where = "All barns" if state == ALL_STATES else state
    # On a long window the headline is a BLEND, and says so in the tile itself
    # rather than only in a caption below the fold. See LONG_WINDOWS.
    long_window = days in LONG_WINDOWS
    avg_sub = ("$/cwt, pound-weighted — blended over the "
               f"{WINDOW_LABEL[days].replace('last ', '')}, not today's price"
               if long_window else "$/cwt, pound-weighted")
    k = st.columns(4)
    with k[0]:
        st.markdown(tile(f"{where} average" + (f", {WINDOW_LABEL[days]}" if long_window else ""),
                         f"${summ['price']:,.2f}",
                         _sub(avg_sub)), unsafe_allow_html=True)
    with k[1]:
        st.markdown(tile("Per head",
                         f"${summ['price'] * summ['weight'] / 100:,.2f}",
                         _sub(f"at {summ['weight']:,.0f} lb average")),
                    unsafe_allow_html=True)
    with k[2]:
        st.markdown(tile("Head sold", f"{summ['head']:,}",
                         _sub(f"{summ['barns']} barn"
                              f"{'s' if summ['barns'] != 1 else ''}")),
                    unsafe_allow_html=True)
    with k[3]:
        st.markdown(tile("Most recent", _fmt_date(summ["last"]),
                         _sub(f"over the {WINDOW_LABEL[days]}")),
                    unsafe_allow_html=True)

    # The spread is worth as much as the average. Measured 2026-09-15, the 600 lb
    # bracket ran $415.43 at Crawford NE against $364.89 at Dunlap IA -- $50/cwt,
    # about $315 a head, on the same weight in the same month.
    if len(rows) > 1:
        hi = max(rows, key=lambda r: r["price"])
        lo = min(rows, key=lambda r: r["price"])
        gap = hi["price"] - lo["price"]
        st.caption(
            f"Spread across {'the ' + str(len(rows)) + ' barns you picked' if picked else 'barns'}"
            f" is **\\${gap:,.2f}/cwt** — {hi['barn']} "
            f"({hi['state']}) at **\\${hi['price']:,.2f}** down to {lo['barn']} "
            f"({lo['state']}) at **\\${lo['price']:,.2f}**, about "
            f"**\\${gap * summ['weight'] / 100:,.0f} a head** on the same "
            f"weight of cattle."
        )

    back = load_lookbacks(wt, state)

    def _row(r):
        out = {"Sale barn": r["barn"], "State": r["state"], "Head": r["head"],
               "Avg wt": round(r["weight"]), "$/cwt": round(r["price"], 2),
               "$/head": round(r["price"] * r["weight"] / 100, 2)}
        prior = back.get((r["barn"], r["state"]), {})
        for centre, label in LOOKBACKS:
            # None, NOT 0.0 and not the current price. pandas renders it blank
            # and leaves the column numeric; a zero would sort to the bottom and
            # read as a $427 collapse.
            p = prior.get(centre)
            out[label] = round(p, 2) if p is not None else None
        out["Prints"] = r["prints"]
        out["Last sale"] = _fmt_date(r["last"])
        return out

    st.dataframe(pd.DataFrame([_row(r) for r in rows]),
                 use_container_width=True, hide_index=True)
    if picked:
        st.caption(
            f"Showing **{len(rows)} of {summ_all['barns']} barns** — the "
            f"{'one' if len(picked) == 1 else 'ones'} you picked. The tiles, "
            f"the spread and this table are over that selection only; clear "
            f"the box to go back to every barn."
        )
    if dropped:
        st.caption(
            f"**{', '.join(dropped)}** {'was' if len(dropped) == 1 else 'were'} "
            f"dropped from the selection: no {wt}-{wt + 49} lb steer prints "
            f"{'in ' + state + ' ' if state != ALL_STATES else ''}in the "
            f"{WINDOW_LABEL[days]}. Check the State box first — it narrows this "
            f"list before the window does."
        )

    if long_window:
        st.warning(
            f"**Every price above is an average over the "
            f"{WINDOW_LABEL[days].replace('last ', '')}**, including the $/cwt "
            f"column — not what cattle are bringing now. Over that span the "
            f"market moves: on 550-600 lb calves the monthly average ran $396.75 "
            f"in August against $479.99 in April. For the current market use a "
            f"14- or 30-day window; for what a barn brought at a point in the "
            f"past use the **{LOOKBACKS[0][1]}** and **{LOOKBACKS[1][1]}** "
            f"columns, which are not affected by this setting."
        )

    st.caption(
        f"Steers only, muscle grade #1 and #1-2, {wt}-{wt + 49} lb, from the "
        f"same USDA AMS barn reports behind the CME Feeder Cattle Index. "
        f"Pound-weighted, so a 300-head draft counts for more than a 4-head "
        f"pen. **Prints** is how many separate lots make up each row — a barn "
        f"showing one print is one lot, not a market. Dated by the auction's "
        f"own sale date."
    )
    st.caption(
        f"**{LOOKBACKS[0][1]}** and **{LOOKBACKS[1][1]}** are that barn's own "
        f"pound-weighted average over a {LOOKBACK_BAND_DAYS}-day band centred "
        f"on the date, at the same weight — not a window average, so they are "
        f"comparable to the $/cwt beside them. Blank means the barn had no "
        f"qualifying prints then: barns open, close and move sale days, and an "
        f"absent comparison is left absent rather than filled in."
    )
