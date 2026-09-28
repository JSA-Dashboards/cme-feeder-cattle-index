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
WINDOWS = [14, 30, 60, 90]


def _conn():
    import snowflake_db as _db
    return _db, _db.get_conn()


def _since(_db, days):
    return (f"DATEADD(day, -{int(days)}, CURRENT_DATE())" if _db.use_snowflake()
            else f"date('now','-{int(days)} day')")


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

    f1, f2, f3 = st.columns([1, 1, 1.4])
    with f1:
        wt = st.selectbox("Weight class", CASH_BRACKETS,
                          index=CASH_BRACKETS.index(600),
                          format_func=lambda w: f"{w}-{w + 49} lb",
                          key=f"{key_prefix}_wt")
    with f3:
        days = st.selectbox("Window", WINDOWS, index=1,
                            format_func=lambda d: f"last {d} days",
                            key=f"{key_prefix}_days")
    with f2:
        state = st.selectbox("State", [ALL_STATES] + load_states(days),
                             key=f"{key_prefix}_state")

    rows, summ = load_rows(wt, state, days)
    if not summ:
        st.info(
            f"No {wt}-{wt + 49} lb steer sales reported in "
            f"{'any tracked state' if state == ALL_STATES else state} over the "
            f"last {days} days. Light calves thin out badly in late summer and "
            f"the heaviest brackets only trade at a few barns — try a wider "
            f"window or a different weight."
        )
        return

    where = "All barns" if state == ALL_STATES else state
    k = st.columns(4)
    with k[0]:
        st.markdown(tile(f"{where} average", f"${summ['price']:,.2f}",
                         _sub("$/cwt, pound-weighted")), unsafe_allow_html=True)
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
                         _sub(f"over the last {days} days")),
                    unsafe_allow_html=True)

    # The spread is worth as much as the average. Measured 2026-09-15, the 600 lb
    # bracket ran $415.43 at Crawford NE against $364.89 at Dunlap IA -- $50/cwt,
    # about $315 a head, on the same weight in the same month.
    if len(rows) > 1:
        hi = max(rows, key=lambda r: r["price"])
        lo = min(rows, key=lambda r: r["price"])
        gap = hi["price"] - lo["price"]
        st.caption(
            f"Spread across barns is **\\${gap:,.2f}/cwt** — {hi['barn']} "
            f"({hi['state']}) at **\\${hi['price']:,.2f}** down to {lo['barn']} "
            f"({lo['state']}) at **\\${lo['price']:,.2f}**, about "
            f"**\\${gap * summ['weight'] / 100:,.0f} a head** on the same "
            f"weight of cattle."
        )

    st.dataframe(pd.DataFrame([
        {"Sale barn": r["barn"], "State": r["state"], "Head": r["head"],
         "Avg wt": round(r["weight"]), "$/cwt": round(r["price"], 2),
         "$/head": round(r["price"] * r["weight"] / 100, 2),
         "Prints": r["prints"], "Last sale": _fmt_date(r["last"])}
        for r in rows]), use_container_width=True, hide_index=True)

    st.caption(
        f"Steers only, muscle grade #1 and #1-2, {wt}-{wt + 49} lb, from the "
        f"same USDA AMS barn reports behind the CME Feeder Cattle Index. "
        f"Pound-weighted, so a 300-head draft counts for more than a 4-head "
        f"pen. **Prints** is how many separate lots make up each row — a barn "
        f"showing one print is one lot, not a market. Dated by the auction's "
        f"own sale date."
    )
