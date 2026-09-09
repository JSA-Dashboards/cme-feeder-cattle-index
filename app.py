import os
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import openpyxl
from pathlib import Path
from datetime import datetime, timedelta

from dotenv import load_dotenv

load_dotenv()

# On Streamlit Community Cloud, secrets live in st.secrets rather than a
# local .env file -- forward anything relevant into os.environ so
# snowflake_db.py (which only reads via os.environ/os.getenv) sees them the
# same way whether running locally or deployed. Mirrors the pattern already
# proven for basis-tracker-streamlit/river-fob-portal.
try:
    for _secret_key in (
        "USE_SNOWFLAKE", "SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD",
        "SNOWFLAKE_ROLE", "SNOWFLAKE_WAREHOUSE", "SNOWFLAKE_DATABASE", "SNOWFLAKE_SCHEMA",
    ):
        if _secret_key in st.secrets and not os.environ.get(_secret_key):
            os.environ[_secret_key] = str(st.secrets[_secret_key])
except Exception:
    pass  # st.secrets not available (no secrets.toml locally) -- fine, .env already loaded above

import snowflake_db as db
from bucketing import shifted_bucket_date

FORECAST_HORIZON_DAYS = 10  # business days
FORECAST_CI = 0.80  # 80% prediction interval

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
FAVICON_PATH = Path(__file__).parent / "assets" / "jsa_favicon.png"
DATA_PATH = Path(__file__).parent / "data" / "Feeder Cattle Info Ross.xlsx"
MARS_DB_PATH = Path(__file__).parent / "data" / "mars_history.db"

VALID_STATES = {"CO", "IA", "KS", "MO", "MT", "NE", "NM", "ND", "OK", "SD", "TX", "WY"}
# Video-auction rows are tagged with a sale region ("North Central"/"South
# Central") instead of a real state code -- without these, they're silently
# excluded from the default Sale Locations/Basis Leaderboard filter even
# though they correctly contribute to fci_value upstream.
VALID_STATES |= {"North Central", "South Central"}

# ── Page Config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="JSA CME Feeder Cattle Index",
    page_icon=str(FAVICON_PATH),
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
  div[class*="st-key-wm-"] {{ position:relative; }}
  div[class*="st-key-wm-"]::after {{
    content:"";
    position:absolute; inset:0; margin:auto;
    width:50%; height:110px; max-width:320px;
    background-image:url('{JSA_LOGO}');
    background-size:contain; background-repeat:no-repeat; background-position:center;
    opacity:0.05; pointer-events:none; z-index:3;
  }}
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


def add_watermark(fig, size=0.32, opacity=0.06):
    fig.add_layout_image(dict(
        source=JSA_LOGO,
        xref="paper", yref="paper", x=0.5, y=0.5,
        xanchor="center", yanchor="middle",
        sizex=size, sizey=size,
        opacity=opacity, layer="below",
    ))
    return fig


@st.cache_data(ttl=3600, show_spinner=False)
def compute_forecast(fci_values, last_date, horizon=FORECAST_HORIZON_DAYS, ci=FORECAST_CI):
    """
    Naive (random-walk) trend projection, flat at the current value, with a
    band built from the historical distribution of actual h-day-ahead price
    changes -- not a fitted statistical model. Backtested against Holt's
    exponential smoothing (66 rolling-origin trials, 2023-10 to 2026-08):
    simple "no change" persistence beat Holt at every forecast horizon
    (overall MAE $3.71 vs $4.07/cwt) -- this series behaves close to a
    random walk, where trend-extrapolation added no real edge, so the
    simpler and more accurate approach is used here instead. This is
    descriptive of typical historical variability, not a trading signal or
    market forecast. Returns None if there isn't enough history.
    """
    y = pd.Series(fci_values).astype(float).reset_index(drop=True)
    if len(y) < 60:
        return None
    current = y.iloc[-1]
    lo_q, hi_q = (1 - ci) / 2, 1 - (1 - ci) / 2
    future_dates = pd.bdate_range(start=pd.Timestamp(last_date) + pd.Timedelta(days=1), periods=horizon)
    rows = []
    for h, d in enumerate(future_dates, start=1):
        changes = (y - y.shift(h)).dropna()
        if len(changes) < 20:
            lower = upper = current
        else:
            lower = current + changes.quantile(lo_q)
            upper = current + changes.quantile(hi_q)
        rows.append({"date": d, "forecast": current, "lower": lower, "upper": upper})
    return pd.DataFrame(rows)


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

    raw_fci = raw[raw["location"] == "FCI"][["date", "price"]].sort_values("date")
    # The sheet has exactly one duplicate-dated FCI row (2024-07-23: 258.39 vs
    # a clearly erroneous 326.18, a lone data-entry duplicate, not a pattern)
    # -- picking blindly via keep="first"/"last" is a coin flip on which one
    # survives, so instead keep whichever candidate is closest to the median
    # of the surrounding +/-5 days' (non-duplicate) values.
    dupe_dates = raw_fci[raw_fci.duplicated(subset="date", keep=False)]["date"].unique()
    if len(dupe_dates):
        single = raw_fci[~raw_fci["date"].isin(dupe_dates)].set_index("date")["price"]
        keep_rows = []
        for d in dupe_dates:
            candidates = raw_fci[raw_fci["date"] == d]
            window = single[(single.index >= d - pd.Timedelta(days=5)) & (single.index <= d + pd.Timedelta(days=5))]
            ref = window.median() if not window.empty else candidates["price"].median()
            keep_rows.append((candidates["price"] - ref).abs().idxmin())
        raw_fci = pd.concat([raw_fci[~raw_fci["date"].isin(dupe_dates)], raw_fci.loc[keep_rows]])

    fci = (
        raw_fci.rename(columns={"price": "fci_value"})
        .sort_values("date")
        .reset_index(drop=True)
    )
    fci["source"] = "workbook"
    # same-day (non-rolling) snapshot isn't in this sheet either
    fci["same_day_price"] = pd.NA
    fci["same_day_head"] = pd.NA
    fci["same_day_avg_weight"] = pd.NA

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
    fci_cols = ["date", "fci_value", "source", "same_day_price", "same_day_head", "same_day_avg_weight"]
    loc_cols = ["date", "location", "state", "price", "head", "avg_weight", "fci_value", "basis", "source"]
    if not db.use_snowflake() and not MARS_DB_PATH.exists():
        return pd.DataFrame(columns=fci_cols), pd.DataFrame(columns=loc_cols)

    conn = db.get_conn()
    fci_raw = db.read_sql_lower(
        "SELECT report_date AS date, fci_value, same_day_price, same_day_head, same_day_avg_weight "
        "FROM fci_daily", conn,
    )
    sales = db.read_sql_lower(
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

    # Bucket exactly as recompute_fci_daily() does. Without this the Sale
    # Locations table and the 7-day window show Clovis on the Wednesday USDA
    # reported while the index counts it on CME's Thursday -- a 47-head
    # disagreement between the rows and the number beside them. raw_date is
    # untouched, so USDA's true date is still recorded.
    sales["date"] = [shifted_bucket_date(l, d)
                     for l, d in zip(sales["location"], sales["date"])]
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


def _load_cme_official():
    """
    CME's own literal daily settlement-calculation files (see cme_ftp.py /
    backfill_ftp.py), pulled from a public FTP archive -- not a
    reconstruction or approximation of any kind, just the exact numbers CME
    itself published. Wherever this covers a date, it should win over every
    other source (workbook, precursor, MARS reconstruction) for that date.

    CME publishes with a lag of roughly 1-3 business days, so this table
    will always be missing the most recent day or two -- that gap is exactly
    where the MARS/Direct/Video reconstruction in _load_mars_reconstruction()
    still earns its keep: estimating what CME will say before it says it,
    not re-deriving history CME has already told us.
    """
    fci_cols = ["date", "fci_value", "source", "same_day_price", "same_day_head", "same_day_avg_weight"]
    loc_cols = ["date", "location", "state", "price", "head", "avg_weight", "fci_value", "basis", "source"]
    empty = (pd.DataFrame(columns=fci_cols), pd.DataFrame(columns=loc_cols))
    if not db.use_snowflake() and not MARS_DB_PATH.exists():
        return empty

    conn = db.get_conn()
    try:
        fci_raw = db.read_sql_lower(
            "SELECT report_date AS date, fci_value, same_day_price, same_day_head, same_day_avg_weight "
            "FROM cme_ftp_daily", conn,
        )
        loc_raw = db.read_sql_lower(
            "SELECT report_date AS date, location, state, head_count, avg_weight, avg_price "
            "FROM cme_ftp_locations", conn,
        )
    except pd.errors.DatabaseError:
        return empty  # table doesn't exist yet -- backfill_ftp.py hasn't run
    finally:
        conn.close()

    if fci_raw.empty:
        return empty

    fci = fci_raw.copy()
    fci["date"] = pd.to_datetime(fci["date"])
    fci["source"] = "cme_official"
    fci = fci.sort_values("date").reset_index(drop=True)

    loc = loc_raw.copy()
    loc["date"] = pd.to_datetime(loc["date"])
    loc = loc.rename(columns={"avg_price": "price", "head_count": "head"})
    loc = loc.merge(fci[["date", "fci_value"]], on="date", how="left")
    loc["basis"] = loc["price"] - loc["fci_value"]
    loc["source"] = "cme_official"
    loc = loc.dropna(subset=["fci_value"])[loc_cols]

    return fci, loc


WB_PRECURSOR_SHEET = "CME Feeder Cattle Index Values"
BRACKET_SPECS = [
    (700, "1"), (750, "1"), (800, "1"), (850, "1"),
    (700, "1-2"), (750, "1-2"), (800, "1-2"), (850, "1-2"),
]


def _parse_bracket_cell(cell):
    """
    Cells hold 'head weight price' as three whitespace-separated numbers, but
    weight and price are sometimes concatenated with no separator when weight
    has decimals (e.g. '172  812.90257.25') -- always exactly two 6-char
    'DDD.DD' halves in that case (weight ~700-899 lbs, price ~$150-450/cwt,
    both always 3 integer digits + 2 decimals in this sheet). Cells with no
    sale in that bracket are '0   0   0.00' or a literal '//////' placeholder.
    """
    if cell is None:
        return None
    parts = str(cell).strip().split()
    try:
        if len(parts) == 3:
            head, weight, price = int(float(parts[0])), float(parts[1]), float(parts[2])
        elif len(parts) == 2:
            head = int(float(parts[0]))
            if head == 0:
                return None
            merged = parts[1]
            if len(merged) != 12:
                return None
            weight, price = float(merged[:6]), float(merged[6:])
        else:
            return None
    except ValueError:
        return None
    if head <= 0 or weight <= 0 or price <= 0:
        return None
    return head, weight, price


@st.cache_data(ttl=3600, show_spinner=False)
def _load_workbook_precursor(before_date):
    """
    'CME Feeder Cattle Index Values' is Ross's raw per-location, per-weight-
    bracket sale data (2023-01-24 - 2026-01-23) -- the actual source data
    behind the workbook's published FCI column, not a separate estimate.
    Recomputing CME's 7-day rolling weighted-average methodology from these
    raw rows reproduces the published index almost exactly (median abs error
    ~$0.003/cwt across the full 2024-2026 overlap, spot-checked 2026-08-26)
    -- far more accurate than the USDA MARS reconstruction for the same kind
    of gap, since this is the real underlying data rather than an
    approximation from a different source. Used only for dates before
    `before_date` (the main workbook sheet's own start), extending the
    ground-truth-quality range back to 2023-01-24.
    """
    fci_cols = ["date", "fci_value", "source", "same_day_price", "same_day_head", "same_day_avg_weight"]
    loc_cols = ["date", "location", "state", "price", "head", "avg_weight", "fci_value", "basis", "source"]
    try:
        wb = openpyxl.load_workbook(DATA_PATH, read_only=True, data_only=True)
        ws = wb[WB_PRECURSOR_SHEET]
    except Exception:
        return pd.DataFrame(columns=fci_cols), pd.DataFrame(columns=loc_cols)

    by_day = {}
    by_day_loc = {}
    for r in ws.iter_rows(values_only=True):
        if not isinstance(r[0], datetime):
            continue
        d, loc, state = r[0].date(), r[1], r[2]
        if not loc or not state:
            continue
        for bi in range(8):
            parsed = _parse_bracket_cell(r[3 + bi])
            if parsed is None:
                continue
            head, weight, price = parsed
            w = head * weight
            wp = w * price
            by_day.setdefault(d, []).append((w, wp, head))
            by_day_loc.setdefault((d, loc, state), []).append((w, wp, head))
    wb.close()

    if not by_day:
        return pd.DataFrame(columns=fci_cols), pd.DataFrame(columns=loc_cols)

    # Drop leading report dates isolated by a large gap from the next one --
    # a single stray early report (e.g. one location, months before dense
    # coverage resumes) isn't a real sample of the 12-state index, and
    # leaving it in would make the chart draw a straight line across the gap
    # to the next real point, fabricating a multi-month "trend" that never
    # happened. Found via real data: one 2023-01-24 report, then a 251-day
    # gap before continuous coverage starts 2023-10-02.
    STALE_GAP_DAYS = 30
    report_dates = sorted(by_day)
    while len(report_dates) >= 2 and (report_dates[1] - report_dates[0]).days > STALE_GAP_DAYS:
        stale = report_dates.pop(0)
        del by_day[stale]
    if not by_day:
        return pd.DataFrame(columns=fci_cols), pd.DataFrame(columns=loc_cols)

    before = pd.Timestamp(before_date).date()
    first_date, last_date = min(by_day), max(by_day)
    fci_rows = []
    d = first_date
    while d <= last_date:
        if d >= before:
            d += timedelta(days=1)
            continue
        window = [d - timedelta(days=i) for i in range(7)]
        num = den = 0.0
        for wd in window:
            for w, wp, head in by_day.get(wd, []):
                den += w
                num += wp
        if den <= 0:
            d += timedelta(days=1)
            continue
        sd = by_day.get(d, [])
        sd_den = sum(w for w, wp, head in sd)
        sd_num = sum(wp for w, wp, head in sd)
        sd_head = sum(head for w, wp, head in sd)
        fci_rows.append({
            "date": pd.Timestamp(d), "fci_value": num / den, "source": "workbook_precursor",
            "same_day_price": (sd_num / sd_den) if sd_den > 0 else pd.NA,
            "same_day_head": sd_head if sd_head > 0 else pd.NA,
            "same_day_avg_weight": (sd_den / sd_head) if sd_head > 0 else pd.NA,
        })
        d += timedelta(days=1)
    fci = pd.DataFrame(fci_rows, columns=fci_cols)
    if fci.empty:
        return fci, pd.DataFrame(columns=loc_cols)
    fci_by_date = dict(zip(fci["date"], fci["fci_value"]))

    loc_rows = []
    for (d, loc, state), rows in by_day_loc.items():
        if d >= before:
            continue
        ts = pd.Timestamp(d)
        if ts not in fci_by_date:
            continue
        den = sum(w for w, wp, head in rows)
        num = sum(wp for w, wp, head in rows)
        head_total = sum(head for w, wp, head in rows)
        if den <= 0 or head_total <= 0:
            continue
        price = num / den
        loc_rows.append({
            "date": ts, "location": str(loc).strip().upper(), "state": str(state).strip().upper(),
            "price": price, "head": head_total, "avg_weight": den / head_total,
            "fci_value": fci_by_date[ts], "basis": price - fci_by_date[ts], "source": "workbook_precursor",
        })
    loc = pd.DataFrame(loc_rows, columns=loc_cols)
    return fci, loc


@st.cache_data(ttl=3600, show_spinner=False)
def _load_recon_index():
    """
    JSA's own reconstruction for EVERY date, including dates CME has since
    published. load_data() deliberately drops those (CME's own value wins for
    display, and rightly so), but scoring the forecast needs BOTH numbers for
    the same date -- otherwise there is no way to see whether the estimates
    were any good.
    """
    empty = pd.DataFrame(columns=["date", "recon", "total_head"])
    if not db.use_snowflake() and not MARS_DB_PATH.exists():
        return empty
    conn = db.get_conn()
    try:
        df = db.read_sql_lower(
            "SELECT report_date AS date, fci_value, total_head FROM fci_daily", conn
        )
    except Exception:
        return empty
    finally:
        conn.close()
    if df.empty:
        return empty
    df["date"] = pd.to_datetime(df["date"])
    return (df.rename(columns={"fci_value": "recon"})
              .sort_values("date").reset_index(drop=True))


@st.cache_data(ttl=3600, show_spinner=False)
def _load_peer_estimates():
    """
    Competitors' published FCI estimates, hand-entered via
    add_peer_estimate.py. index_date is CME's index date, so these line up
    directly with fci_daily and cme_ftp_daily.
    """
    empty = pd.DataFrame(columns=["date", "source", "value"])
    if not db.use_snowflake() and not MARS_DB_PATH.exists():
        return empty
    conn = db.get_conn()
    try:
        df = db.read_sql_lower(
            "SELECT index_date AS date, source, fci_value FROM peer_estimates", conn)
    except Exception:
        return empty          # table absent on this backend yet
    finally:
        conn.close()
    if df.empty:
        return empty
    df["date"] = pd.to_datetime(df["date"])
    return df.rename(columns={"fci_value": "value"})


@st.cache_data(ttl=3600, show_spinner=False)
def _load_cme_index_dates():
    """
    Every date CME has actually filed an index for, straight from its own
    file archive (3,000+ files back to 2015).

    This replaces a USFederalHolidayCalendar, which was simply wrong about
    this series. Measured over 2024-01-01..2026-09-04, CME published on 22 of
    28 weekday federal holidays -- MLK, Presidents Day, Juneteenth, Columbus
    Day, Veterans Day, New Year's Day, July 4 2025, and Labor Day in both
    2024 and 2025. It skipped only 11 weekdays in that span, several of which
    are not federal holidays at all (Dec 24, Dec 26, Dec 31, Jul 3 2025). The
    real skip set is roughly Memorial Day, Independence Day, Thanksgiving and
    the Christmas-New Year stretch, and it is not even consistent year to
    year: Memorial Day 2024 was skipped, 2025 and 2026 were not.

    No fixed rule reproduces that. CME's own history does, by construction.
    """
    empty = pd.DatetimeIndex([])
    if not db.use_snowflake() and not MARS_DB_PATH.exists():
        return empty
    conn = db.get_conn()
    try:
        df = db.read_sql_lower("SELECT report_date AS date FROM cme_ftp_daily", conn)
    except Exception:
        return empty
    finally:
        conn.close()
    if df.empty:
        return empty
    return pd.DatetimeIndex(pd.to_datetime(df["date"])).sort_values()


@st.cache_data(ttl=3600, show_spinner=False)
def load_data():
    """
    Priority order, earliest ground-truth-quality data wins for each date:
      1. cme_official: CME's own exact daily settlement files (cme_ftp.py) --
         wins over every other source for any date it covers. Published
         with a ~1-3 business day lag, so this always trails off a bit short
         of today.
      2. wb_fci (2024-01-01 - 2026-01-23): the workbook's published CME
         values -- ground truth, kept as a fallback for any date the FTP
         archive itself doesn't cover.
      3. precursor (2023-01-24 - 2023-12-31): recomputed from Ross's raw
         per-location sale data using CME's own methodology -- validated to
         within about $0.20/cwt (median $0.003) of the published column
         where the two overlap, so treated as ground-truth-equivalent.
      4. mars_before / mars_after: JSA's own USDA MARS reconstruction --
         this is the ONLY genuine estimate left in this chain, now that
         cme_official covers almost all already-published history. Its real
         job is the trailing day or two CME hasn't published yet -- an
         actual prediction, not a stand-in for data CME has already
         released.
    """
    wb_fci, wb_loc = _load_workbook()
    wb_start, wb_end = wb_fci["date"].min(), wb_fci["date"].max()

    precursor_fci, precursor_loc = _load_workbook_precursor(wb_start)
    precursor_start = precursor_fci["date"].min() if not precursor_fci.empty else wb_start

    mars_fci, mars_loc = _load_mars_reconstruction()
    mars_before_fci = mars_fci[mars_fci["date"] < precursor_start]
    mars_before_loc = mars_loc[mars_loc["date"] < precursor_start]
    mars_after_fci = mars_fci[mars_fci["date"] > wb_end]
    mars_after_loc = mars_loc[mars_loc["date"] > wb_end]

    fallback_fci = pd.concat(
        [mars_before_fci, precursor_fci, wb_fci, mars_after_fci], ignore_index=True
    )
    fallback_loc = pd.concat(
        [mars_before_loc, precursor_loc, wb_loc, mars_after_loc], ignore_index=True
    )

    official_fci, official_loc = _load_cme_official()
    if not official_fci.empty:
        fallback_fci = fallback_fci[~fallback_fci["date"].isin(official_fci["date"])]
        fallback_loc = fallback_loc[~fallback_loc["date"].isin(official_fci["date"])]

    fci = pd.concat([fallback_fci, official_fci], ignore_index=True).sort_values("date").reset_index(drop=True)
    loc = pd.concat([fallback_loc, official_loc], ignore_index=True).sort_values("date").reset_index(drop=True)

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

    _SOURCE_LABELS = {
        "cme_official": "CME's own published daily settlement file — exact, not a reconstruction.",
        "workbook": "JSA-compiled workbook, CME's published index value.",
        "workbook_precursor": "JSA reconstruction from Ross's raw per-location sale data (the same source behind the published column) — validated within about $0.20/cwt of published values where they overlap.",
        "usda_mars": "JSA's own USDA MARS/Direct/Video estimate — CME hasn't published this date yet (usually a 1-3 business day lag), so this is a genuine forecast, not a stand-in for released data.",
    }
    # Weekday-only for this range display: CME never publishes a file for
    # Sat/Sun, so every weekend date falls back to the MARS estimate even in
    # an otherwise fully-official stretch, scattering "usda_mars" rows across
    # nearly two years and making a naive min/max span look like a much
    # wider estimate window than the real one (which is just the trailing
    # 1-3 unpublished business days). The underlying fci_df/chart still
    # correctly includes those weekend estimate rows -- only this summary
    # range ignores them.
    _segments = sorted(
        (wd["date"].min(), wd["date"].max(), src)
        for src, grp in fci_df.groupby("source")
        for wd in [grp[grp["date"].dt.weekday < 5]]
        if not wd.empty
    )
    _seg_html = "".join(
        f'<b>{start.strftime("%b %d, %Y")} – {end.strftime("%b %d, %Y")}:</b><br>'
        f'{_SOURCE_LABELS.get(src, src)}<br><br>'
        for start, end, src in _segments
    )

    st.markdown("<hr>", unsafe_allow_html=True)
    st.markdown(
        f'<div style="color:{MUTED};font-size:0.72rem;line-height:1.6;">'
        f'Sample: 12-state feeder steer region<br>'
        f'(CO, IA, KS, MO, MT, NE, NM, ND, OK, SD, TX, WY)<br><br>'
        f'Grade/weight: #1 &amp; #1-2 Steers, Medium &amp; Large,<br>'
        f'700–899 lbs, FOB 3% standing shrink<br><br>'
        f'Coverage: {first_date.strftime("%b %d, %Y")} – {last_date.strftime("%b %d, %Y")}<br><br>'
        + _seg_html
        + f'All segments are drawn as one solid line — not<br>'
        f'visually distinguished, see this panel for which<br>'
        f'dates are which. Run <code>update_index.py</code><br>'
        f'to refresh.'
        f'</div>',
        unsafe_allow_html=True,
    )

loc_filtered = loc_df[loc_df["state"].isin(state_filter)] if state_filter else loc_df
# Guards against a stale cached load_data() result from before these columns
# existed surviving a Streamlit Cloud soft-redeploy (in-memory cache, ttl=1hr).
for _col in ("head", "avg_weight"):
    if _col not in loc_filtered.columns:
        loc_filtered[_col] = pd.NA


# ── Header ────────────────────────────────────────────────────────────────────

c1, c2 = st.columns([7, 3])
with c1:
    st.markdown(
        f"<h1 style='color:{TEXT};margin:0;padding:0;font-size:1.9rem;'>"
        "JSA - CME Feeder Cattle Index</h1>"
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

def _round2(v):
    return None if v is None or pd.isna(v) else round(v, 2)


def _mdy(d):
    """M/D/YY, matching the tile style CIH and CME's own sheets use."""
    return "%d/%d/%s" % (d.month, d.day, d.strftime("%y"))


# CME FILES an index under the date its sales run through, then RELEASES it the
# following business day. Those two differ by more than a day whenever a holiday
# intervenes: the 9/4/2026 file was released 9/8, because 9/5-9/6 were the
# weekend and 9/7 was Labor Day.
#
# Everything user-facing on this page is labelled by CME's own INDEX date -- the
# last sale day in the window, which is how CME names its files -- with the
# release date shown alongside as context.
#
# Release-date labelling was tried on 2026-09-09 and reverted the same day. It
# reads more naturally in isolation, being the print people wait for, but CIH's
# daily sheet -- which this desk checks against every morning -- is dated by
# INDEX date. Release labelling put every number here one business day ahead of
# that benchmark, turning each comparison into a mental subtraction, and it also
# disagreed with CME's filenames and the raw data table.
#
# No single label satisfies every consumer: QST charts the same index against
# what looks like the date it received each value, so CME's 9/3 index appears
# there on a 9/8 bar. That is why both dates are shown rather than one.
_CME_INDEX_DATES = _load_cme_index_dates()

# Raw internal source strings are meaningless on screen, and without them there
# is no way to tell a published CME value from a JSA estimate in the raw table.
_SOURCE_LABELS = {
    "cme_official": "CME published",
    "workbook": "CME published (workbook)",
    "usda_mars": "JSA estimate",
}


def _release_date(d):
    """
    When CME releases the index it filed under `d`: the next date CME files
    an index for, read off its own history rather than guessed from a holiday
    calendar.

    Past the end of that history there is nothing to read, so this falls back
    to the next weekday. That is a guess, and it is the one place this column
    can be wrong -- but it is the right guess for Labor Day, which CME filed
    through in both 2024 and 2025.
    """
    if d is None or pd.isna(d):
        return None
    ts = pd.Timestamp(d)
    i = _CME_INDEX_DATES.searchsorted(ts, side="right")
    if i < len(_CME_INDEX_DATES):
        return _CME_INDEX_DATES[i]
    return ts + pd.offsets.BDay(1)



# Round each individual value to display precision BEFORE differencing, not
# after -- otherwise a change tile can show e.g. -$0.10 while the two values
# it's derived from display as $329.20 and $329.31 (an $0.11 difference by
# eye), since -0.1003 and -0.11 round to different cents even though they're
# both "correct" in isolation. Rounding first keeps every number on screen
# self-consistent with the others.
current = _round2(fci_df.iloc[-1]["fci_value"])
# Adjacent-row change, kept separate from the "Last CME Print" tile below so
# the " DoD" caption stays a genuine day-over-day rather than spanning the
# gap back to CME's last publication.
_adjacent = _round2(fci_df.iloc[-2]["fci_value"]) if len(fci_df) > 1 else None
day_chg = _round2(current - _adjacent) if _adjacent is not None else None

# "Current Index" is only accurate when the latest date is a real published
# value (source == "workbook" or "cme_official"). Any other date is JSA's
# own reconstruction -- label it as an estimate so it's never mistaken for
# the real published figure.
#
# Labelled with the DATE OF THE DATA, not the calendar. This used to date the
# estimate to TODAY, on the reasoning that a carry-forward figure is our best
# guess "for today". That convention breaks as soon as publication lags more
# than a day: over the 2026 Labor Day weekend it captioned a window ending
# 9/4 as "FCI Estimate 9/8/26", three days off, and nothing on screen said
# which date the number was actually for. CME labels each index by the date
# its sales run THROUGH and releases it the following afternoon, so the data
# date is both the honest label and the one that lines up with CME's own
# print and with CIH's daily sheet.
_cur_row = fci_df.iloc[-1]
current_label = (
    f"Current Index ({_mdy(_cur_row['date'])})"
    if _cur_row["source"] in ("workbook", "cme_official")
    else f"FCI Estimate {_mdy(_cur_row['date'])}"
)

week_ago = _round2(value_on_or_before(fci_df.iloc[:-1], last_date - timedelta(days=7)))
week_chg = _round2(current - week_ago) if week_ago is not None else None

month_ago = _round2(value_on_or_before(fci_df.iloc[:-1], last_date - timedelta(days=30)))
month_chg = _round2(current - month_ago) if month_ago is not None else None

year_ago = _round2(value_on_or_before(fci_df.iloc[:-1], last_date - timedelta(days=365)))
year_chg = _round2(current - year_ago) if year_ago is not None else None

# Labeled as TODAY minus one calendar day, not the date of whichever row
# prev_point actually comes from -- same carry-forward convention as
# current_label above. Once CME actually publishes that date, the label and
# the underlying data date will naturally line up; until then this reads
# "yesterday" even if the value shown is itself carried forward further back.
# This tile is CME's last ACTUAL print, so its value, its label and its delta
# all come from one place: the most recent cme_official row. It previously
# showed fci_df.iloc[-2] -- whatever row happened to be second-to-last --
# while captioning the delta "CME DoD". That is fine while the
# reconstruction sits one day ahead of CME, but the moment it runs several
# days past CME's last publication (which is exactly what a holiday does to
# the lag) the tile shows an ESTIMATE under a CME label. Sourcing all three
# from official_rows means the label and the number cannot diverge.
official_rows = fci_df[fci_df["source"] == "cme_official"]
cme_actual_chg = None
if len(official_rows) > 1:
    cme_actual_chg = _round2(official_rows.iloc[-1]["fci_value"] - official_rows.iloc[-2]["fci_value"])

if len(official_rows):
    prev_point = _round2(official_rows.iloc[-1]["fci_value"])
    prev_label = f"Last CME Print ({_mdy(official_rows.iloc[-1]['date'])})"
else:
    prev_point = None
    prev_label = "Last CME Print"

cols = st.columns(4)
with cols[0]:
    st.markdown(tile(current_label, fmt_price(current), delta_html(day_chg, " DoD")), unsafe_allow_html=True)
with cols[1]:
    st.markdown(tile(prev_label, fmt_price(prev_point), delta_html(cme_actual_chg, " CME DoD")), unsafe_allow_html=True)
with cols[2]:
    st.markdown(tile("Week Change", fmt_price(week_chg), delta_html(week_chg)), unsafe_allow_html=True)
with cols[3]:
    st.markdown(tile("Month Change", fmt_price(month_chg), delta_html(month_chg)), unsafe_allow_html=True)

# ── Daily (same-day, non-rolling) snapshot ─────────────────────────────────────
# Mirrors the "Daily: $X on Y head and Z lbs average" line under CME subscriber
# reports — the single date's own weighted average, distinct from the 7-day
# rolling Current Index above it.
last_row = fci_df.iloc[-1]
sd_price = last_row.get("same_day_price")
sd_head = last_row.get("same_day_head")
sd_weight = last_row.get("same_day_avg_weight")
if pd.notna(sd_price) and pd.notna(sd_head):
    weight_part = f" and <b style='color:{TEXT}'>{sd_weight:.0f} lbs</b> average" if pd.notna(sd_weight) else ""
    st.markdown(
        f"<div style='color:{MUTED};font-size:0.82rem;margin-top:10px;'>"
        f"Daily: <b style='color:{TEXT}'>${sd_price:.2f}</b> on "
        f"<b style='color:{TEXT}'>{int(sd_head):,}</b> head{weight_part}"
        f"</div>",
        unsafe_allow_html=True,
    )


# ── 7-Day Window (rolling index composition) ──────────────────────────────────
# Mirrors the top "Daily Totals" box in Compass's own report -- shows the
# individual days feeding the rolling Current Index above, so it's clear
# how that single number was built. Always unfiltered by state, same as
# the KPI tiles it explains.

st.markdown("<div style='height:18px'></div>", unsafe_allow_html=True)
st.markdown('<div class="sec-header">7-Day Window (Rolling Index Composition)</div>', unsafe_allow_html=True)

window_start = last_date - timedelta(days=6)
window_fci = fci_df[(fci_df["date"] >= window_start) & (fci_df["date"] <= last_date)][
    ["date", "same_day_price", "same_day_head", "same_day_avg_weight"]
].copy().sort_values("date").reset_index(drop=True)

# Saturday/Sunday sales are already folded into the following Monday's own
# same_day_* figures (see update_index.py's recompute_fci_daily -- confirmed
# against CME's own official files, a Monday's DAILY TOTALS literally equals
# Monday's own rows plus the preceding Saturday's). Sat/Sun therefore never
# carry their own same-day total; showing them as separate blank rows here
# just looks like missing data. Drop them and relabel Monday's row to show
# the span it actually represents, matching how CME's own report has no
# standalone Sat/Sun line at all.
drop_idx = []
for i, row in window_fci.iterrows():
    if row["date"].weekday() == 5:  # Saturday
        span_start = row["date"]
        j = i
        while j + 1 < len(window_fci) and window_fci.loc[j + 1, "date"].weekday() in (5, 6):
            j += 1
        if j + 1 < len(window_fci):
            drop_idx.extend(range(i, j + 1))
            window_fci.loc[j + 1, "_span_start"] = span_start
window_fci = window_fci.drop(index=drop_idx).reset_index(drop=True)
window_fci["Day"] = window_fci.apply(
    lambda r: f'{r["_span_start"].strftime("%a %m/%d")}–{r["date"].strftime("%a %m/%d")}'
    if "_span_start" in window_fci.columns and pd.notna(r.get("_span_start"))
    else r["date"].strftime("%a %m/%d"),
    axis=1,
)

window_loc = loc_df[(loc_df["date"] >= window_start) & (loc_df["date"] <= last_date)]
window_w = window_loc["head"] * window_loc["avg_weight"]
window_total_head = window_loc["head"].sum()
window_total_weight = (window_w.sum() / window_total_head) if window_total_head else None

window_totals_row = pd.DataFrame([{
    "Day": "7-DAY TOTAL",
    "same_day_head": window_total_head if window_total_head else pd.NA,
    "same_day_avg_weight": window_total_weight,
    "same_day_price": current,
}])
window_disp = pd.concat(
    [window_fci[["Day", "same_day_head", "same_day_avg_weight", "same_day_price"]], window_totals_row],
    ignore_index=True,
).rename(columns={"same_day_head": "Head", "same_day_avg_weight": "Weight", "same_day_price": "Price"})

with st.container(key="wm-window"):
    st.dataframe(
        window_disp.style.format({
            "Head": "{:,.0f}", "Weight": "{:,.0f} lb", "Price": "${:.2f}",
        }, na_rep="—").apply(
            lambda row: ["font-weight:700;border-top:2px solid " + BORDER] * len(row)
            if row["Day"] == "7-DAY TOTAL" else [""] * len(row),
            axis=1,
        ),
        use_container_width=True, hide_index=True, height=320,
    )
st.caption(
    "Each row is that day's own weighted average (not rolling) — together they're the raw material "
    "behind the rolling Current Index above. Saturday/Sunday sales are combined into the following "
    "Monday's row (CME's own convention, confirmed against their published files) rather than shown "
    "separately. A blank row means that location's next scheduled sale hasn't landed yet."
)


# ── FCI Trend Chart ───────────────────────────────────────────────────────────

st.markdown("<div style='height:18px'></div>", unsafe_allow_html=True)
st.markdown('<div class="sec-header">Index Trend</div>', unsafe_allow_html=True)

AXIS = dict(
    gridcolor=BORDER, linecolor=BORDER, showgrid=True,
    tickfont=dict(color=MUTED, size=11),
    title_font=dict(color=MUTED, size=11),
    zeroline=False,
)

hover_source = fci_df["source"].map({
    "workbook": "Published",
    "workbook_precursor": "JSA reconstruction (Ross data)",
    "usda_mars": "JSA reconstruction (USDA MARS)",
})

forecast_df = compute_forecast(fci_df["fci_value"], last_date)

fig = go.Figure()
fig.add_trace(go.Scatter(
    x=fci_df["date"], y=fci_df["fci_value"],
    customdata=hover_source,
    name="CME Feeder Cattle Index", mode="lines",
    line=dict(color=JPSI_BLUE, width=2),
    hovertemplate="<b>%{customdata}</b>: $%{y:.2f}<extra></extra>",
))
if forecast_df is not None:
    connector = pd.concat([
        pd.DataFrame({"date": [last_date], "forecast": [current], "lower": [current], "upper": [current]}),
        forecast_df,
    ], ignore_index=True)
    fig.add_trace(go.Scatter(
        x=pd.concat([connector["date"], connector["date"][::-1]]),
        y=pd.concat([connector["upper"], connector["lower"][::-1]]),
        fill="toself", fillcolor="rgba(230,126,34,0.15)",
        line=dict(color="rgba(0,0,0,0)"),
        hoverinfo="skip", showlegend=False, name="Forecast band",
    ))
    fig.add_trace(go.Scatter(
        x=connector["date"], y=connector["forecast"],
        name="Naive forecast (no-change)", mode="lines",
        line=dict(color="#e67e22", width=2, dash="dash"),
        hovertemplate="<b>Forecast</b>: $%{y:.2f}<extra></extra>",
    ))
fig.update_layout(
    paper_bgcolor=BG, plot_bgcolor=BG,
    font=dict(color=TEXT, size=11),
    hovermode="x unified",
    showlegend=forecast_df is not None,
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
add_watermark(fig, size=0.34, opacity=0.055)
st.plotly_chart(fig, use_container_width=True)
caption_bits = [
    "Line covers JSA's compiled workbook (published CME values) plus JSA's own USDA MARS "
    "reconstruction on both ends of that range — see the sidebar for methodology and accuracy notes."
]
if forecast_df is not None:
    caption_bits.append(
        f"Dashed orange segment is a {FORECAST_HORIZON_DAYS}-business-day naive (no-change) "
        f"projection with a {FORECAST_CI:.0%} band from historical day-ahead variability — backtested "
        "against a trend-following model and this simpler approach was actually more accurate (this "
        "series moves close to a random walk), but it's still not a trading signal or market forecast."
    )
st.caption(" ".join(caption_bits))


# ── Seasonal Pattern ──────────────────────────────────────────────────────────

st.markdown("<div style='height:18px'></div>", unsafe_allow_html=True)
st.markdown('<div class="sec-header">Seasonal Pattern by Year</div>', unsafe_allow_html=True)

seas = fci_df[["date", "fci_value"]].copy()
seas["year"] = seas["date"].dt.year
seas["doy"] = seas["date"].dt.dayofyear
current_year = seas["year"].max()

fig_seas = go.Figure()
for yr, grp in seas.groupby("year"):
    is_current = yr == current_year
    fig_seas.add_trace(go.Scatter(
        x=grp["doy"], y=grp["fci_value"],
        name=str(yr), mode="lines",
        line=dict(color=JPSI_BLUE if is_current else None, width=3 if is_current else 1.5),
        opacity=1.0 if is_current else 0.55,
        hovertemplate=f"<b>{yr}</b>: $%{{y:.2f}}<extra></extra>",
    ))
fig_seas.update_layout(
    paper_bgcolor=BG, plot_bgcolor=BG,
    font=dict(color=TEXT, size=11),
    hovermode="x unified",
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1,
                font=dict(color=MUTED, size=10), bgcolor="rgba(0,0,0,0)"),
    margin=dict(l=55, r=20, t=15, b=40),
    xaxis=dict(**AXIS, title="Day of Year"),
    yaxis=dict(**AXIS, title="$/cwt", tickprefix="$"),
    height=380,
)
add_watermark(fig_seas, size=0.3, opacity=0.06)
st.plotly_chart(fig_seas, use_container_width=True)
st.caption(
    f"Each line is one calendar year plotted by day-of-year ({current_year} bolded) — shows where "
    "this year sits against the same point in prior years. Not detrended: absolute levels differ "
    "year to year with broader market conditions, not just seasonality."
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

with st.container(key="wm-weekly"):
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

day_fci = value_on_or_before(fci_df, detail_date)

# Strictly same-day: only locations with an actual report dated detail_date.
# (A carry-forward version of this table was tried -- showing each location's
# most recent report within the trailing 7-day window -- to mirror how a
# Wednesday-only auction like Clovis NM still appears on Compass's later
# reports. It was reverted: summed across the full roster the carry-forward
# total ballooned into a "total receipts"-looking number (e.g. 11,589 head)
# that didn't correspond to any real day's volume and didn't reconcile with
# Compass either. This table should show what actually reported today.)
day_rows = loc_filtered[loc_filtered["date"] == detail_date].copy()
if not day_rows.empty and day_fci is not None:
    day_rows["basis"] = day_rows["price"] - day_fci
day_rows = day_rows.sort_values("basis", ascending=False)

if day_rows.empty:
    st.info("No reporting sale locations on this date. Pick another date in the sidebar.")
else:
    left, right = st.columns([3, 2])
    with left:
        # Weighted totals row (matches how Compass's own report closes each
        # day's location table) -- weighted by head*weight, same formula as
        # the FCI itself, not a plain average of the Price column.
        total_head = day_rows["head"].sum()
        w = day_rows["head"] * day_rows["avg_weight"]
        total_weight = (w.sum() / total_head) if total_head else None
        total_price = ((w * day_rows["price"]).sum() / w.sum()) if w.sum() else None
        totals_row = pd.DataFrame([{
            "location": "TOTAL", "state": "", "date": pd.NaT,
            "head": total_head if total_head else pd.NA,
            "avg_weight": total_weight, "price": total_price,
            "basis": (total_price - day_fci) if (total_price is not None and day_fci is not None) else None,
        }])
        day_rows_with_total = pd.concat([day_rows, totals_row], ignore_index=True)

        disp = day_rows_with_total[["location", "state", "head", "avg_weight", "price", "basis"]].rename(columns={
            "location": "Location", "state": "State", "head": "Head", "avg_weight": "Weight",
            "price": "Price", "basis": "Basis vs FCI",
        })
        with st.container(key="wm-locations"):
            st.dataframe(
                disp.style.format({
                    "Head": "{:,.0f}", "Weight": "{:,.0f} lb", "Price": "${:.2f}", "Basis vs FCI": "{:+.2f}",
                }, na_rep="—").map(
                    lambda v: f"color: {POS}" if isinstance(v, (int, float)) and v > 0
                    else (f"color: {NEG}" if isinstance(v, (int, float)) and v < 0 else ""),
                    subset=["Basis vs FCI"],
                ).apply(
                    lambda row: ["font-weight:700;border-top:2px solid " + BORDER] * len(row)
                    if row["Location"] == "TOTAL" else [""] * len(row),
                    axis=1,
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
        add_watermark(fig_b, size=0.4, opacity=0.06)
        st.plotly_chart(fig_b, use_container_width=True)

    # Two distinct numbers, same distinction as the KPI section above but for
    # whichever date is being browsed here: the true same-day figure (this
    # date's own fresh sales only) and the rolling FCI (7-day window) used
    # for the Basis column -- labeled "Published Index" instead of "FCI
    # Estimate" on dates where that rolling value is CME's real published
    # number, not JSA's reconstruction.
    detail_rows = fci_df[fci_df["date"] == detail_date]
    detail_is_published = not detail_rows.empty and detail_rows.iloc[0]["source"] in ("workbook", "cme_official")
    fci_label = "Published Index" if detail_is_published else "FCI Estimate"
    sd_price_d = detail_rows.iloc[0]["same_day_price"] if not detail_rows.empty else None
    sd_head_d = detail_rows.iloc[0]["same_day_head"] if not detail_rows.empty else None
    sd_weight_d = detail_rows.iloc[0]["same_day_avg_weight"] if not detail_rows.empty else None

    caption_parts = []
    if pd.notna(sd_price_d) and pd.notna(sd_head_d):
        weight_part_d = f" and <b style='color:{TEXT}'>{sd_weight_d:.0f} lbs</b> average" if pd.notna(sd_weight_d) else ""
        caption_parts.append(
            f"Daily: <b style='color:{TEXT}'>${sd_price_d:.2f}</b> on "
            f"<b style='color:{TEXT}'>{int(sd_head_d):,}</b> head{weight_part_d}"
        )
    if day_fci is not None:
        caption_parts.append(f"{fci_label}: <b style='color:{TEXT}'>${day_fci:.2f}</b>")
    if caption_parts:
        st.markdown(
            f"<div style='color:{MUTED};font-size:0.82rem;margin-top:8px;'>"
            + " &nbsp;·&nbsp; ".join(caption_parts) + "</div>",
            unsafe_allow_html=True,
        )


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
    add_watermark(fig_lb, size=0.3, opacity=0.06)
    st.plotly_chart(fig_lb, use_container_width=True)
    st.caption("Locations with at least 3 reported sales in the trailing 90 days, strongest and weakest basis shown.")


# ── Data Table ────────────────────────────────────────────────────────────────

# ── Pending CME Prints / Forecast Scorecard ───────────────────────────────────
# The headline tile only ever shows the LATEST date, which is not the number
# you want when using this as a forecast. What matters is (a) which dates CME
# still owes a print for, with our estimate for each, and (b) how close the
# last several estimates actually landed. Without this, both required either
# hovering the trend chart or reading the raw table and knowing from memory
# where CME's published history stops.

st.markdown("<div style='height:18px'></div>", unsafe_allow_html=True)
st.markdown('<div class="sec-header">Pending CME Prints &amp; Forecast Accuracy</div>',
            unsafe_allow_html=True)
st.caption(
    "Dated by CME **index** date — the last sale day in the 7-day window, which is "
    "how CME names its files and how CIH dates its daily sheet. *Prints* is when "
    "CME releases it: the next business day, which a holiday can push days out."
)

_recon = _load_recon_index()
_official = (
    fci_df[fci_df["source"] == "cme_official"][["date", "fci_value"]]
    .rename(columns={"fci_value": "actual"})
)

if _recon.empty:
    st.info("No reconstruction available on this backend, so there is nothing to compare.")
else:
    _sc = _recon.merge(_official, on="date", how="left")
    _last_official = _official["date"].max() if not _official.empty else None

    # Only dates AFTER CME's last print are genuinely pending. An unmatched
    # date before that is a day CME simply does not publish (weekend/holiday),
    # not a forecast awaiting a result.
    _pending = _sc[_sc["actual"].isna()]
    if _last_official is not None:
        _pending = _pending[_pending["date"] > _last_official]
    # Weekdays only. This reconstruction computes a value for every CALENDAR
    # day, and CME never files one for a Saturday or Sunday, so the weekend
    # carry-forwards are not prints anyone is waiting for.
    #
    # This deliberately does NOT exclude holidays. An earlier version filtered
    # on a federal-holiday business-day calendar, which hid the 9/7/2026
    # estimate as a Labor Day -- but CME filed an index on Labor Day in both
    # 2024 and 2025 (see _load_cme_index_dates), so 9/7 is a print that is
    # genuinely outstanding, not one CME declined to make. Showing a date CME
    # later turns out to skip is the cheaper error: it drops out of this table
    # on its own once CME's frontier moves past it.
    _pending = _pending[_pending["date"].map(
        lambda d: pd.Timestamp(d).weekday() < 5)]

    _c1, _c2 = st.columns(2)
    with _c1:
        st.caption("**Awaiting CME** — our forecast for each unpublished index date")
        if _pending.empty:
            st.caption("CME has published every date we hold an estimate for.")
        else:
            _p = _pending.sort_values("date", ascending=False).copy()
            _p["Index date"] = _p["date"].dt.strftime("%a %m/%d")
            _p["Prints"] = _p["date"].map(lambda d: _release_date(d).strftime("%m/%d"))
            _p["JSA FCI EST"] = _p["recon"].map(lambda v: f"${v:.2f}")
            _p["Head"] = _p["total_head"].map(
                lambda v: f"{v:,.0f}" if pd.notna(v) else "—")
            with st.container(key="wm-pending"):
                st.dataframe(_p[["Index date", "Prints", "JSA FCI EST", "Head"]],
                             use_container_width=True, hide_index=True, height=210)
            if _last_official is not None:
                st.caption(
                    f"CME's last index date is {_last_official.strftime('%b %d')} "
                    f"(printed {_release_date(_last_official).strftime('%b %d')}). A low "
                    "*window head* means few sale days are in the 7-day window yet, so "
                    "that estimate will move as reports land."
                )
    with _c2:
        st.caption("**Scorecard** — how the last ten estimates turned out")
        _s = _sc.dropna(subset=["actual"]).sort_values("date", ascending=False).head(10).copy()
        if _s.empty:
            st.caption("No dates where both a reconstruction and a CME print exist.")
        else:
            _s["err"] = _s["recon"] - _s["actual"]
            _s["Index date"] = _s["date"].dt.strftime("%m/%d")
            _s["JSA FCI EST"] = _s["recon"].map(lambda v: f"${v:.2f}")
            _s["CME"] = _s["actual"].map(lambda v: f"${v:.2f}")
            _s["Miss"] = _s["err"].map(lambda v: f"{v:+.2f}")
            with st.container(key="wm-scored"):
                st.dataframe(_s[["Index date", "JSA FCI EST", "CME", "Miss"]],
                             use_container_width=True, hide_index=True, height=210)
            # Dollar signs escaped: st.caption renders markdown, and a $...$
            # pair is LaTeX math there -- unescaped, "$0.38" and "$2" render as
            # mangled math rather than money.
            st.caption(
                f"Mean absolute miss over these {len(_s)} dates: "
                f"**\\${_s['err'].abs().mean():.2f}**. Dates before the direct-trade "
                "component began (2026-08-28) ran about \\$2 high because that input "
                "was missing entirely -- they are not representative of current accuracy."
            )


# ── Versus the competition ────────────────────────────────────────────────────
# The scorecard above answers "are we close to CME". This answers "are we
# closer than the desks we compete with", which is a different question and the
# one that actually matters commercially. Their figures are hand-entered from
# their daily sheets, so this table is only as complete as what has been typed
# in -- dates with no peer figure are simply absent rather than shown as zero.

_peers = _load_peer_estimates()
if not _peers.empty:
    st.markdown("<div style='height:18px'></div>", unsafe_allow_html=True)
    st.markdown('<div class="sec-header">Versus CIH &amp; Compass</div>',
                unsafe_allow_html=True)
    st.caption(
        "Their published estimate against ours for the same CME index date. "
        "*Miss* columns appear once CME prints that date; before then all three "
        "are open forecasts."
    )

    _piv = _peers.pivot_table(index="date", columns="source", values="value",
                              aggfunc="last")
    _srcs = [c for c in sorted(_piv.columns)]
    _ours_s = (_recon.set_index("date")["recon"] if not _recon.empty
               else pd.Series(dtype=float))
    _cme_s = (official_rows.set_index("date")["fci_value"] if len(official_rows)
              else pd.Series(dtype=float))

    _t = _piv.copy()
    _t["__ours"] = _ours_s
    _t["__cme"] = _cme_s
    _t = _t.sort_index(ascending=False)

    # Three labels per source, because the value column, the miss column and
    # the summary caption each want a different length -- and because "CIH"
    # must never go through .title(), which renders it "Cih".
    _SRC_LABELS = {
        "CIH":     ("CIH FCI EST",     "CIH FCI EST Miss",     "CIH"),
        "COMPASS": ("Compass FCI EST", "Compass FCI EST Miss", "Compass"),
    }
    _labels = lambda src: _SRC_LABELS.get(
        src, (f"{src.title()} FCI EST", f"{src.title()} FCI EST Miss", src.title()))
    _lbl = lambda src: _labels(src)[0]        # value column
    _miss_lbl = lambda src: _labels(src)[1]   # miss column
    _short = lambda src: _labels(src)[2]      # caption, where a full header is noise
    _money = lambda v: f"${v:.2f}" if pd.notna(v) else "—"
    _delta = lambda v: f"{v:+.2f}" if pd.notna(v) else "—"

    _disp = pd.DataFrame(index=_t.index)
    _disp["Index date"] = _t.index.strftime("%a %m/%d")
    _disp["JSA FCI EST"] = _t["__ours"].map(_money)
    for _s in _srcs:
        _disp[_lbl(_s)] = _t[_s].map(_money)
    _disp["CME"] = _t["__cme"].map(_money)
    _disp["JSA FCI EST Miss"] = (_t["__ours"] - _t["__cme"]).map(_delta)
    for _s in _srcs:
        _disp[_miss_lbl(_s)] = (_t[_s] - _t["__cme"]).map(_delta)

    with st.container(key="wm-peers"):
        st.dataframe(_disp, use_container_width=True, hide_index=True,
                     height=min(320, 60 + 35 * len(_disp)))

    # Running accuracy, over scored dates only. Each source is averaged over
    # the dates IT has a figure for, so the counts can differ -- shown, because
    # "0.01 over 7 dates" and "0.01 over 1 date" are not the same claim.
    _scored = _t[_t["__cme"].notna()]
    if len(_scored):
        _bits = []
        _o = (_scored["__ours"] - _scored["__cme"]).abs().dropna()
        if len(_o):
            _bits.append(f"JSA {_o.mean():.3f} ({len(_o)})")
        for _s in _srcs:
            _e = (_scored[_s] - _scored["__cme"]).abs().dropna()
            if len(_e):
                _bits.append(f"{_short(_s)} {_e.mean():.3f} ({len(_e)})")
        st.caption("Mean absolute miss, dates scored in brackets: " + " · ".join(_bits))
    else:
        st.caption("No date here has been printed by CME yet, so nobody is scored.")


with st.expander("📋  Raw Data Table"):
    tab_fci, tab_loc = st.tabs(["Index Values", "Location Sales"])
    with tab_fci:
        # Plain string formatting instead of pandas Styler -- with 10+ years
        # of history now in scope, these tables can exceed Styler's
        # styler.render.max_elements cell cap (hit at ~280k cells testing
        # the location table below), and no conditional coloring is applied
        # here anyway, just number formatting.
        #
        # "Date" is CME's index date, matching the tiles and both panel tables.
        # "Prints" is when CME releases it. "Source" used to render as a raw
        # internal string (usda_mars / cme_official), which gave no way to tell an
        # estimate from a published value -- the single most important thing to
        # know when reading this tab, and the reason it was easy to mistake a
        # forecast for a settled number.
        d = fci_df.copy()
        d["Prints"] = d["date"].map(
            lambda x: _release_date(x).strftime("%Y-%m-%d") if pd.notna(x) else "—")
        d["Source"] = d["source"].map(_SOURCE_LABELS).fillna(d["source"])
        d["date"] = d["date"].dt.strftime("%Y-%m-%d")
        d = d.rename(columns={
            "date": "Date", "fci_value": "FCI", "same_day_price": "Daily $",
            "same_day_head": "Daily head", "same_day_avg_weight": "Daily wt",
        }).sort_values("Date", ascending=False)
        d["FCI"] = d["FCI"].map(lambda v: f"${v:.2f}" if pd.notna(v) else "—")
        d["Daily $"] = d["Daily $"].map(lambda v: f"${v:.2f}" if pd.notna(v) else "—")
        d["Daily head"] = d["Daily head"].map(lambda v: f"{v:,.0f}" if pd.notna(v) else "—")
        d["Daily wt"] = d["Daily wt"].map(lambda v: f"{v:,.0f} lb" if pd.notna(v) else "—")
        with st.container(key="wm-raw-fci"):
            st.dataframe(
                d[["Date", "Prints", "Source", "FCI", "Daily $", "Daily head", "Daily wt"]],
                use_container_width=True, hide_index=True, height=320)
        st.caption(
            "**Date** is CME's index date — the last sale day in that 7-day window. "
            "**Prints** is when CME releases it, the next business day. **Source** "
            "separates CME's published values from JSA's own estimates; only the "
            "estimates are forecasts."
        )
    with tab_loc:
        d = loc_filtered[["date", "location", "state", "head", "avg_weight", "price", "fci_value", "basis"]].copy()
        d["date"] = d["date"].dt.strftime("%Y-%m-%d")
        d = d.rename(columns={
            "date": "Date", "location": "Location", "state": "State", "head": "Head",
            "avg_weight": "Weight", "price": "Price", "fci_value": "FCI", "basis": "Basis",
        }).sort_values("Date", ascending=False)
        d["Head"] = d["Head"].map(lambda v: f"{v:,.0f}" if pd.notna(v) else "—")
        d["Weight"] = d["Weight"].map(lambda v: f"{v:,.0f} lb" if pd.notna(v) else "—")
        d["Price"] = d["Price"].map(lambda v: f"${v:.2f}" if pd.notna(v) else "—")
        d["FCI"] = d["FCI"].map(lambda v: f"${v:.2f}" if pd.notna(v) else "—")
        d["Basis"] = d["Basis"].map(lambda v: f"{v:+.2f}" if pd.notna(v) else "—")
        with st.container(key="wm-raw-loc"):
            st.dataframe(d, use_container_width=True, hide_index=True, height=320)


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
