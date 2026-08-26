import sqlite3
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from pathlib import Path
from datetime import datetime, timedelta

# ── JSA Brand Colors ─────────────────────────────────────────────────────────
JPSI_DARK = "#32373c"
JPSI_BLUE = "#0693e3"
BG        = "#ffffff"
CARD_BG   = "#ffffff"
BORDER    = "#e6eaee"
MUTED     = "#6b7280"
TEXT      = "#1f2328"
POS       = "#0d7f3d"
NEG       = "#c00000"
NEU       = "#6b7280"

JSA_LOGO = "https://www.jpsi.com/wp-content/themes/gate39media/img/logo-full.png"
DATA_PATH = Path(__file__).parent / "data" / "Feeder Cattle Info Ross.xlsx"
MARS_DB_PATH = Path(__file__).parent / "data" / "mars_history.db"

VALID_STATES = {"CO", "IA", "KS", "MO", "MT", "NE", "NM", "ND", "OK", "SD", "TX", "WY"}

# ── Page Config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="JSA CME Feeder Cattle Index",
    page_icon="🐂",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(f"""
<style>
  html, body, [data-testid="stAppViewContainer"] {{
    background-color:{BG}; color:{TEXT};
  }}
  [data-testid="stSidebar"] {{
    background-color:#f6f8fa; border-right:1px solid {BORDER};
  }}
  .tile {{
    background:{CARD_BG}; border:1px solid {BORDER};
    border-top:3px solid {JPSI_BLUE}; border-radius:10px;
    padding:16px 20px; text-align:center; height:100%;
    box-shadow: 0 1px 2px rgba(0,0,0,0.04);
  }}
  .tile-label {{
    color:{MUTED}; font-size:0.68rem; text-transform:uppercase;
    letter-spacing:0.09em; margin-bottom:6px;
  }}
  .tile-value {{
    color:{TEXT}; font-size:1.7rem; font-weight:700; line-height:1.1;
  }}
  .tile-delta-pos {{ color:{POS}; font-size:0.82rem; font-weight:600; margin-top:4px; }}
  .tile-delta-neg {{ color:{NEG}; font-size:0.82rem; font-weight:600; margin-top:4px; }}
  .tile-delta-neu {{ color:{NEU}; font-size:0.82rem; font-weight:600; margin-top:4px; }}
  .sec-header {{
    color:{MUTED}; font-size:0.72rem; text-transform:uppercase;
    letter-spacing:0.1em; padding:8px 0 4px; border-bottom:1px solid {BORDER};
    margin-bottom:10px;
  }}
  hr {{ border-color:{BORDER}; }}
  #MainMenu, footer {{ visibility:hidden; }}
  .stDeployButton {{ display:none; }}
</style>
""", unsafe_allow_html=True)


# ── Helpers ───────────────────────────────────────────────────────────────────

def delta_html(val, suffix=""):
    if val is None or pd.isna(val):
        return '<div class="tile-delta-neu">—</div>'
    sign = "▲" if val > 0 else ("▼" if val < 0 else "")
    color = "pos" if val > 0 else ("neg" if val < 0 else "neu")
    return f'<div class="tile-delta-{color}">{sign} ${abs(val):.2f}{suffix}</div>'


def tile(label, value, delta=""):
    return (f'<div class="tile">'
            f'<div class="tile-label">{label}</div>'
            f'<div class="tile-value">{value}</div>'
            f'{delta}</div>')


def fmt_price(v):
    if v is None or pd.isna(v):
        return "—"
    return f"-${abs(v):.2f}" if v < 0 else f"${v:.2f}"


def value_on_or_before(df, target_date):
    """Latest fci_value at or before target_date."""
    sub = df[df["date"] <= target_date]
    return sub.iloc[-1]["fci_value"] if not sub.empty else None


# ── Data Loading ──────────────────────────────────────────────────────────────

def _load_workbook():
    """
    Source: 'Sale Location Data 24-25' sheet of the JSA-compiled workbook.
    Each date has one row per reporting sale location (Daily $ price) plus a
    synthetic 'FCI' row holding that date's published CME Feeder Cattle Index
    value. Basis is recomputed here (location price - that day's FCI) so it
    stays consistent even where the source sheet's own Basis column is stale.
    Covers 2024-01-01 through 2026-01-23.
    """
    raw = pd.read_excel(DATA_PATH, sheet_name="Sale Location Data 24-25", usecols="A:D", header=0)
    raw.columns = ["date", "location", "state", "price"]
    raw = raw.dropna(subset=["date", "location", "price"])
    raw["date"] = pd.to_datetime(raw["date"])
    raw["location"] = raw["location"].astype(str).str.strip().str.upper()
    raw["state"] = raw["state"].astype(str).str.strip().str.upper()

    fci = (
        raw[raw["location"] == "FCI"][["date", "price"]]
        .rename(columns={"price": "fci_value"})
        .drop_duplicates(subset="date", keep="last")
        .sort_values("date")
        .reset_index(drop=True)
    )
    fci["source"] = "workbook"

    loc = raw[raw["location"] != "FCI"].copy()
    loc = loc.merge(fci[["date", "fci_value"]], on="date", how="left")
    loc["basis"] = loc["price"] - loc["fci_value"]
    loc = loc.dropna(subset=["fci_value"])
    loc["source"] = "workbook"
    # head count / avg weight aren't in this sheet — the location table shows "—" for these dates
    loc["head"] = pd.NA
    loc["avg_weight"] = pd.NA

    return fci, loc


def _load_mars_reconstruction():
    """
    Continues the timeline past the workbook's last date (2026-01-23) using
    USDA AMS MARS sale-barn data, reconstructed with the same weighted-average
    methodology as the workbook's own 'FCI Estimation' sheet:

        FCI(date) = sum(head*weight*price) / sum(head*weight)

    across a ~60-location roster in the CME 12-state region (see
    update_index.py / data/mars_roster.json). This is JSA's own reconstruction,
    not CME's official feed, and has been spot-checked against CME's published
    values within roughly $2-9/cwt on any given day (missing Direct/Video/
    Internet trade volume, which this sale-barn-only roster doesn't capture).
    Run `python update_index.py` to refresh.
    """
    loc_cols = ["date", "location", "state", "price", "head", "avg_weight", "fci_value", "basis", "source"]
    if not MARS_DB_PATH.exists():
        return pd.DataFrame(columns=["date", "fci_value", "source"]), pd.DataFrame(columns=loc_cols)

    conn = sqlite3.connect(MARS_DB_PATH)
    fci_raw = pd.read_sql("SELECT report_date AS date, fci_value FROM fci_daily", conn)
    sales = pd.read_sql(
        "SELECT report_date AS date, location, state, weight_low, head_count, avg_weight, avg_price "
        "FROM mars_sales", conn,
    )
    conn.close()

    fci = fci_raw.copy()
    fci["date"] = pd.to_datetime(fci["date"])
    fci["source"] = "usda_mars"
    fci = fci.sort_values("date").reset_index(drop=True)

    if sales.empty:
        loc = pd.DataFrame(columns=loc_cols)
        return fci, loc

    sales["date"] = pd.to_datetime(sales["date"])
    sales["w"] = sales["head_count"] * sales["avg_weight"]
    sales["wp"] = sales["w"] * sales["avg_price"]
    daily_loc = (
        sales.groupby(["date", "location", "state"])
        .agg(w=("w", "sum"), wp=("wp", "sum"), head=("head_count", "sum"))
        .reset_index()
    )
    daily_loc["price"] = daily_loc["wp"] / daily_loc["w"]
    # weighted-average weight across whatever brackets/grades a location reported that day
    daily_loc["avg_weight"] = daily_loc["w"] / daily_loc["head"]
    loc = daily_loc.merge(fci[["date", "fci_value"]], on="date", how="left")
    loc["basis"] = loc["price"] - loc["fci_value"]
    loc["source"] = "usda_mars"
    loc = loc.dropna(subset=["fci_value"])[loc_cols]

    return fci, loc


@st.cache_data(ttl=3600, show_spinner=False)
def load_data():
    wb_fci, wb_loc = _load_workbook()
    mars_fci, mars_loc = _load_mars_reconstruction()

    cutoff = wb_fci["date"].max()
    mars_fci = mars_fci[mars_fci["date"] > cutoff]
    mars_loc = mars_loc[mars_loc["date"] > cutoff]

    fci = pd.concat([wb_fci, mars_fci], ignore_index=True).sort_values("date").reset_index(drop=True)
    loc = pd.concat([wb_loc, mars_loc], ignore_index=True).sort_values("date").reset_index(drop=True)

    return fci, loc


with st.spinner("Loading feeder cattle sale data…"):
    try:
        fci_df, loc_df = load_data()
        load_ok = True
        err_msg = ""
    except Exception as e:
        load_ok = False
        err_msg = str(e)
        fci_df, loc_df = pd.DataFrame(), pd.DataFrame()

if not load_ok:
    st.error("Could not load the source workbook.")
    with st.expander("Technical details"):
        st.code(err_msg)
    st.stop()

if fci_df.empty:
    st.warning("No FCI values found in the source data.")
    st.stop()

last_date = fci_df["date"].max()
first_date = fci_df["date"].min()


# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.image(JSA_LOGO, use_container_width=True)
    st.markdown("<hr>", unsafe_allow_html=True)

    st.markdown('<div class="sec-header">Detail Date</div>', unsafe_allow_html=True)
    detail_date = st.date_input(
        "Location detail for",
        value=last_date.date(),
        min_value=first_date.date(),
        max_value=last_date.date(),
        label_visibility="collapsed",
    )
    detail_date = pd.Timestamp(detail_date)

    st.markdown("<hr>", unsafe_allow_html=True)
    st.markdown('<div class="sec-header">State Filter</div>', unsafe_allow_html=True)
    all_states = sorted(loc_df["state"].unique().tolist())
    default_states = [s for s in all_states if s in VALID_STATES] or all_states
    state_filter = st.multiselect("States", all_states, default=default_states, label_visibility="collapsed")

    st.markdown("<hr>", unsafe_allow_html=True)
    if st.button("↺  Refresh Data", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.markdown("<hr>", unsafe_allow_html=True)
    st.markdown(
        f'<div style="color:{MUTED};font-size:0.72rem;line-height:1.6;">'
        f'Sample: 12-state feeder steer region<br>'
        f'(CO, IA, KS, MO, MT, NE, NM, ND, OK, SD, TX, WY)<br><br>'
        f'Grade/weight: #1 &amp; #1-2 Steers, Medium &amp; Large,<br>'
        f'700–899 lbs, FOB 3% standing shrink<br><br>'
        f'Coverage: {first_date.strftime("%b %d, %Y")} – {last_date.strftime("%b %d, %Y")}<br><br>'
        f'<b>Jan 2024 – Jan 23, 2026:</b> JSA-compiled workbook,<br>'
        f'CME\'s published index value.<br><br>'
        f'<b>Jan 24, 2026 – present:</b> JSA reconstruction from<br>'
        f'USDA MARS sale-barn data (~60 locations), same<br>'
        f'weighted-average method. Spot-checked within<br>'
        f'roughly $2–$9/cwt of CME\'s published value —<br>'
        f'not an official CME feed. Run <code>update_index.py</code><br>'
        f'to refresh.'
        f'</div>',
        unsafe_allow_html=True,
    )

loc_filtered = loc_df[loc_df["state"].isin(state_filter)] if state_filter else loc_df


# ── Header ────────────────────────────────────────────────────────────────────

c1, c2 = st.columns([7, 3])
with c1:
    st.markdown(
        f"<h1 style='color:{TEXT};margin:0;padding:0;font-size:1.9rem;'>"
        "CME Feeder Cattle Index</h1>"
        f"<div style='color:{MUTED};font-size:0.8rem;margin-top:2px;'>"
        "12-State Feeder Steer Sample · #1 &amp; #1-2 Medium &amp; Large, 700–899 lbs</div>",
        unsafe_allow_html=True,
    )
with c2:
    st.markdown(
        f"<div style='text-align:right;color:{MUTED};font-size:0.75rem;padding-top:6px;'>"
        f"Most recent index date<br>"
        f"<span style='color:{JPSI_BLUE};font-size:1rem;font-weight:700;'>"
        f"{last_date.strftime('%b %d, %Y')}</span></div>",
        unsafe_allow_html=True,
    )

st.markdown("<hr style='margin:10px 0 18px;'>", unsafe_allow_html=True)


# ── KPI Tiles ─────────────────────────────────────────────────────────────────

current = fci_df.iloc[-1]["fci_value"]
prev_point = fci_df.iloc[-2]["fci_value"] if len(fci_df) > 1 else None
day_chg = current - prev_point if prev_point is not None else None

week_ago = value_on_or_before(fci_df.iloc[:-1], last_date - timedelta(days=7))
week_chg = current - week_ago if week_ago is not None else None

month_ago = value_on_or_before(fci_df.iloc[:-1], last_date - timedelta(days=30))
month_chg = current - month_ago if month_ago is not None else None

year_ago = value_on_or_before(fci_df.iloc[:-1], last_date - timedelta(days=365))
year_chg = current - year_ago if year_ago is not None else None

cols = st.columns(4)
with cols[0]:
    st.markdown(tile("Current Index", fmt_price(current)), unsafe_allow_html=True)
with cols[1]:
    st.markdown(tile("Day Change", fmt_price(day_chg), delta_html(day_chg)), unsafe_allow_html=True)
with cols[2]:
    st.markdown(tile("Week Change", fmt_price(week_chg), delta_html(week_chg)), unsafe_allow_html=True)
with cols[3]:
    st.markdown(tile("Month Change", fmt_price(month_chg), delta_html(month_chg)), unsafe_allow_html=True)


# ── FCI Trend Chart ───────────────────────────────────────────────────────────

st.markdown("<div style='height:18px'></div>", unsafe_allow_html=True)
st.markdown('<div class="sec-header">Index Trend</div>', unsafe_allow_html=True)

AXIS = dict(
    gridcolor=BORDER, linecolor=BORDER, showgrid=True,
    tickfont=dict(color=MUTED, size=11),
    title_font=dict(color=MUTED, size=11),
    zeroline=False,
)

wb_seg = fci_df[fci_df["source"] == "workbook"]
mars_seg = fci_df[fci_df["source"] == "usda_mars"]
# repeat the last workbook point so the reconstruction segment connects with no visual gap
if not wb_seg.empty and not mars_seg.empty:
    mars_seg = pd.concat([wb_seg.tail(1), mars_seg], ignore_index=True)

fig = go.Figure()
fig.add_trace(go.Scatter(
    x=wb_seg["date"], y=wb_seg["fci_value"],
    name="Published (workbook)", mode="lines",
    line=dict(color=JPSI_BLUE, width=2),
    hovertemplate="<b>FCI</b>: $%{y:.2f}<extra></extra>",
))
if not mars_seg.empty:
    fig.add_trace(go.Scatter(
        x=mars_seg["date"], y=mars_seg["fci_value"],
        name="JSA reconstruction (USDA MARS)", mode="lines",
        line=dict(color=JPSI_BLUE, width=2, dash="dot"),
        hovertemplate="<b>Reconstructed FCI</b>: $%{y:.2f}<extra></extra>",
    ))
fig.update_layout(
    paper_bgcolor=BG, plot_bgcolor=BG,
    font=dict(color=TEXT, size=11),
    hovermode="x unified",
    showlegend=not mars_seg.empty,
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1,
                font=dict(color=MUTED, size=10), bgcolor="rgba(0,0,0,0)"),
    margin=dict(l=55, r=20, t=15, b=40),
    xaxis=dict(
        **AXIS, title="",
        rangeselector=dict(
            buttons=[
                dict(count=1, label="1M", step="month", stepmode="backward"),
                dict(count=3, label="3M", step="month", stepmode="backward"),
                dict(count=6, label="6M", step="month", stepmode="backward"),
                dict(count=1, label="YTD", step="year", stepmode="todate"),
                dict(count=1, label="1Y", step="year", stepmode="backward"),
                dict(step="all", label="All"),
            ],
            bgcolor="#f6f8fa", activecolor=JPSI_BLUE,
            font=dict(color=TEXT, size=10), bordercolor=BORDER,
        ),
        rangeslider=dict(visible=False),
        type="date",
    ),
    yaxis=dict(**AXIS, title="$/cwt", tickprefix="$"),
    height=380,
)
st.plotly_chart(fig, use_container_width=True)
if not mars_seg.empty:
    st.caption(
        "Dotted segment (Jan 24, 2026 onward) is JSA's own reconstruction from USDA MARS sale-barn "
        "data, not CME's official published index — see the sidebar for methodology and accuracy notes."
    )


# ── Weekly Rundown ────────────────────────────────────────────────────────────

st.markdown("<div style='height:18px'></div>", unsafe_allow_html=True)
st.markdown('<div class="sec-header">Weekly Rundown</div>', unsafe_allow_html=True)

wk = fci_df.copy()
wk["week_end"] = wk["date"] - pd.to_timedelta(wk["date"].dt.weekday.map(lambda d: (d - 6) % 7), unit="D")
wk_summary = (
    wk.groupby("week_end")
    .agg(week_avg=("fci_value", "mean"), week_last=("fci_value", "last"),
         week_high=("fci_value", "max"), week_low=("fci_value", "min"))
    .reset_index()
    .sort_values("week_end")
)
wk_summary["prior_last"] = wk_summary["week_last"].shift(1)
wk_summary["week_chg"] = wk_summary["week_last"] - wk_summary["prior_last"]

display_wk = wk_summary.tail(10).sort_values("week_end", ascending=False).copy()
display_wk["Week Ending"] = display_wk["week_end"].dt.strftime("%m/%d/%Y")
display_wk = display_wk.rename(columns={
    "week_avg": "Week Avg", "week_last": "Week Last",
    "week_high": "Week High", "week_low": "Week Low", "week_chg": "Week Chg",
})[["Week Ending", "Week Avg", "Week Last", "Week High", "Week Low", "Week Chg"]]

st.dataframe(
    display_wk.style.format({
        "Week Avg": "${:.2f}", "Week Last": "${:.2f}",
        "Week High": "${:.2f}", "Week Low": "${:.2f}", "Week Chg": "{:+.2f}",
    }, na_rep="—").map(
        lambda v: f"color: {POS}" if isinstance(v, (int, float)) and v > 0
        else (f"color: {NEG}" if isinstance(v, (int, float)) and v < 0 else ""),
        subset=["Week Chg"],
    ),
    use_container_width=True, hide_index=True, height=340,
)


# ── Location Detail ───────────────────────────────────────────────────────────

st.markdown("<div style='height:18px'></div>", unsafe_allow_html=True)
st.markdown(
    f'<div class="sec-header">Sale Locations — {detail_date.strftime("%A, %B %d, %Y")}</div>',
    unsafe_allow_html=True,
)

day_rows = loc_filtered[loc_filtered["date"] == detail_date].sort_values("basis", ascending=False)
day_fci = value_on_or_before(fci_df, detail_date)

if day_rows.empty:
    st.info("No reporting sale locations on this date. Pick another date in the sidebar.")
else:
    left, right = st.columns([3, 2])
    with left:
        disp = day_rows[["location", "state", "head", "avg_weight", "price", "basis"]].rename(columns={
            "location": "Location", "state": "State", "head": "Head", "avg_weight": "Weight",
            "price": "Price", "basis": "Basis vs FCI",
        })
        st.dataframe(
            disp.style.format({
                "Head": "{:,.0f}", "Weight": "{:,.0f} lb", "Price": "${:.2f}", "Basis vs FCI": "{:+.2f}",
            }, na_rep="—").map(
                lambda v: f"color: {POS}" if isinstance(v, (int, float)) and v > 0
                else (f"color: {NEG}" if isinstance(v, (int, float)) and v < 0 else ""),
                subset=["Basis vs FCI"],
            ),
            use_container_width=True, hide_index=True, height=380,
        )
    with right:
        fig_b = go.Figure()
        bar_colors = [POS if v >= 0 else NEG for v in day_rows["basis"]]
        fig_b.add_trace(go.Bar(
            x=day_rows["basis"], y=day_rows["location"],
            orientation="h", marker_color=bar_colors,
            hovertemplate="<b>%{y}</b>: %{x:+.2f}<extra></extra>",
        ))
        fig_b.update_layout(
            paper_bgcolor=BG, plot_bgcolor=BG,
            font=dict(color=TEXT, size=10),
            margin=dict(l=10, r=10, t=10, b=30),
            xaxis=dict(**AXIS, title="Basis vs FCI ($/cwt)"),
            yaxis=dict(**AXIS, autorange="reversed"),
            height=380, showlegend=False,
        )
        st.plotly_chart(fig_b, use_container_width=True)
    if day_fci is not None:
        st.caption(f"Index value used for basis: **${day_fci:.2f}**")


# ── Basis Leaderboard ─────────────────────────────────────────────────────────

st.markdown("<div style='height:18px'></div>", unsafe_allow_html=True)
st.markdown('<div class="sec-header">Average Basis by Location (Trailing 90 Days)</div>', unsafe_allow_html=True)

window_start = last_date - timedelta(days=90)
recent = loc_filtered[loc_filtered["date"] >= window_start]
leaderboard = (
    recent.groupby("location")
    .agg(avg_basis=("basis", "mean"), sales=("basis", "size"))
    .query("sales >= 3")
    .sort_values("avg_basis", ascending=False)
    .reset_index()
)

if leaderboard.empty:
    st.info("Not enough recent sales to build a basis leaderboard.")
else:
    top_bottom = pd.concat([leaderboard.head(10), leaderboard.tail(10)]).drop_duplicates(subset="location")
    top_bottom = top_bottom.sort_values("avg_basis", ascending=True)
    fig_lb = go.Figure()
    lb_colors = [POS if v >= 0 else NEG for v in top_bottom["avg_basis"]]
    fig_lb.add_trace(go.Bar(
        x=top_bottom["avg_basis"], y=top_bottom["location"],
        orientation="h", marker_color=lb_colors,
        hovertemplate="<b>%{y}</b>: %{x:+.2f} avg basis<extra></extra>",
    ))
    fig_lb.update_layout(
        paper_bgcolor=BG, plot_bgcolor=BG,
        font=dict(color=TEXT, size=11),
        margin=dict(l=10, r=10, t=10, b=30),
        xaxis=dict(**AXIS, title="Avg Basis vs FCI ($/cwt)"),
        yaxis=dict(**AXIS),
        height=440, showlegend=False,
    )
    st.plotly_chart(fig_lb, use_container_width=True)
    st.caption("Locations with at least 3 reported sales in the trailing 90 days, strongest and weakest basis shown.")


# ── Data Table ────────────────────────────────────────────────────────────────

with st.expander("📋  Raw Data Table"):
    tab_fci, tab_loc = st.tabs(["Index Values", "Location Sales"])
    with tab_fci:
        d = fci_df.copy()
        d["date"] = d["date"].dt.strftime("%Y-%m-%d")
        st.dataframe(
            d.rename(columns={"date": "Date", "fci_value": "FCI"}).sort_values("Date", ascending=False)
            .style.format({"FCI": "${:.2f}"}),
            use_container_width=True, hide_index=True, height=320,
        )
    with tab_loc:
        d = loc_filtered[["date", "location", "state", "head", "avg_weight", "price", "fci_value", "basis"]].copy()
        d["date"] = d["date"].dt.strftime("%Y-%m-%d")
        st.dataframe(
            d.rename(columns={
                "date": "Date", "location": "Location", "state": "State", "head": "Head",
                "avg_weight": "Weight", "price": "Price", "fci_value": "FCI", "basis": "Basis",
            }).sort_values("Date", ascending=False)
            .style.format({
                "Head": "{:,.0f}", "Weight": "{:,.0f} lb", "Price": "${:.2f}", "FCI": "${:.2f}", "Basis": "{:+.2f}",
            }, na_rep="—"),
            use_container_width=True, hide_index=True, height=320,
        )


# ── Footer ────────────────────────────────────────────────────────────────────

_year = datetime.now().year
st.markdown("<hr style='border-color:#e6eaee;margin-top:32px;margin-bottom:16px'>", unsafe_allow_html=True)
st.markdown(
    f'<div style="color:#9aa2ab;font-size:0.68rem;line-height:1.6;text-align:center;padding:0 24px 24px;">'
    f'Historical index and basis figures are derived from JSA-compiled 12-state feeder steer sale data '
    f'(coverage: {first_date.strftime("%b %d, %Y")}–{last_date.strftime("%b %d, %Y")}) and are provided for informational purposes only. '
    f'Trading commodity futures, options on futures, cash commodities, and over-the-counter derivative products involves substantial risk of loss and may not be suitable for all investors. '
    f'This communication does not constitute investment advice, a recommendation, or an offer or solicitation to buy or sell any futures, options, cash commodities, or derivative products. '
    f'John Stewart &amp; Associates, Inc. does not accept orders to buy or sell any financial instruments via email. '
    f'The information contained herein has been obtained from sources believed to be reliable; however, its accuracy and completeness are not guaranteed. '
    f'&copy; John Stewart &amp; Associates, Inc. {_year}'
    f'</div>',
    unsafe_allow_html=True,
)
