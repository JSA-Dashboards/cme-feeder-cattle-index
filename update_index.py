"""
Extends the CME Feeder Cattle Index dashboard's history past where the Ross
workbook stops (2026-01-23) using USDA AMS MARS API data.

Methodology matches CME's own published definition (cmegroup.com): the
index is a rolling SEVEN-CALENDAR-DAY volume-weighted average, not a
same-day snapshot. Every qualifying sale row (Steers, Medium & Large
frame, grade #1 or #1-2, 700-899 lb weight brackets, final reports only —
preliminary excluded) is pulled from a fixed roster of ~60 sale-barn
reports across the CME 12-state region. For each date D:

    FCI(D) = sum(head*weight*price for report_date in [D-6, D])
             / sum(head*weight for report_date in [D-6, D])

Every pound gets equal weight (CME's own wording). Using a single day
instead of the 7-day window was an earlier bug here — with ~60 sale-barn
locations, many reporting only weekly, a single day's sample is thin
(sometimes 1 location), which produced day-to-day noise far larger than
CME's real index shows.

Also pulls Direct Cattle Report PDFs (see direct_reports.py) for the
Direct/Video/Internet trade component of CME's sample -- NOT exposed as
structured data via the MARS API (narrative text only for that report
family), so these are fetched and parsed directly from
ams.usda.gov/mnreports/. Only "Current"-timing, FOB-freight rows qualify
(CME's 14-day pickup / FOB rule); forward-month contracts and delivered
(non-FOB) rows are excluded. These PDFs always show the current week only
(no historical-date parameter), so this component only extends the
dataset forward from whenever it's first run -- it doesn't backfill past
dates the way the auction data's initial run did.

Run manually or on a schedule:
    python update_index.py [--since YYYY-MM-DD]

Requires env var MARS_API_KEY (USDA MARS API key, free registration at
https://mymarketnews.ams.usda.gov/mymarketnews-api).
"""
import argparse
import json
import os
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import requests

from direct_reports import DIRECT_REPORT_SLUGS, fetch_all_direct_rows
from video_reports import VIDEO_REPORT_SLUGS, fetch_all_video_rows

HERE = Path(__file__).parent
DATA_DIR = HERE / "data"
ROSTER_PATH = DATA_DIR / "mars_roster.json"
DB_PATH = DATA_DIR / "mars_history.db"

MARS_BASE = "https://marsapi.ams.usda.gov/services/v1.2"
TARGET_GRADES = {"1", "1-2"}
TARGET_BRACKETS = {700, 750, 800, 850}
CONTINUATION_START = date(2026, 1, 24)  # day after the workbook's last date


def get_auth():
    key = os.environ.get("MARS_API_KEY")
    if not key:
        raise SystemExit("Set MARS_API_KEY in the environment before running.")
    return (key, "")


def init_db(conn):
    # WAL mode lets other processes (Streamlit, backfill_ftp.py) keep
    # reading the DB while this write transaction is open.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mars_sales (
            report_date TEXT NOT NULL,
            raw_date TEXT NOT NULL,
            slug_id INTEGER NOT NULL,
            location TEXT NOT NULL,
            state TEXT NOT NULL,
            weight_low INTEGER NOT NULL,
            muscle_grade TEXT NOT NULL,
            head_count INTEGER NOT NULL,
            avg_weight REAL NOT NULL,
            avg_price REAL NOT NULL,
            PRIMARY KEY (report_date, slug_id, weight_low, muscle_grade, avg_price, head_count)
        )
    """)
    # Migration for DBs created before raw_date existed.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(mars_sales)").fetchall()}
    if "raw_date" not in cols:
        conn.execute("ALTER TABLE mars_sales ADD COLUMN raw_date TEXT")
        conn.execute("UPDATE mars_sales SET raw_date = report_date WHERE raw_date IS NULL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fci_daily (
            report_date TEXT PRIMARY KEY,
            fci_value REAL NOT NULL,
            n_locations INTEGER NOT NULL,
            total_head INTEGER NOT NULL,
            same_day_price REAL,
            same_day_head INTEGER,
            same_day_avg_weight REAL
        )
    """)
    conn.commit()


def fetch_slug(slug_id, since_str, until_str, auth):
    resp = requests.get(
        f"{MARS_BASE}/reports/{slug_id}",
        auth=auth,
        params={"q": f"report_begin_date={since_str}:{until_str}"},
        timeout=(5, 60),
    )
    resp.raise_for_status()
    return resp.json().get("results", [])


def qualifying_rows(rows):
    out = []
    for r in rows:
        if (r.get("class") == "Steers"
                and r.get("frame") == "Medium and Large"
                and r.get("muscle_grade") in TARGET_GRADES
                and r.get("weight_break_low") in TARGET_BRACKETS
                and r.get("final_ind") == "Final"
                and r.get("head_count") and r.get("avg_weight") and r.get("avg_price")):
            out.append(r)
    return out


def mdY(d: date) -> str:
    return d.strftime("%m/%d/%Y")


def shift_weekend_to_monday(d: date) -> date:
    """
    CME's own methodology: Saturday and Sunday sales are treated as the
    following Monday's transactions for the rolling 7-day window (confirmed
    against CME's published rules). A handful of our roster locations
    genuinely sell on Saturday (Ericson NE is a fixed Saturday auction; a
    few others show up as occasional Saturday makeup sales), so this isn't
    a hypothetical edge case -- without it, those sales fall in the wrong
    week's window entirely. Weekday dates pass through unchanged.
    """
    if d.weekday() == 5:  # Saturday
        return d + timedelta(days=2)
    if d.weekday() == 6:  # Sunday
        return d + timedelta(days=1)
    return d


def recompute_fci_daily(conn):
    """
    Recomputes the FULL fci_daily table from ALL stored mars_sales, using a
    rolling 7-calendar-day trailing window per CME's published methodology
    (each date's window can reach up to 6 days before any given `since`, so
    this is NOT limited to a recently-affected date range -- it's cheap
    given table size). Returns (n_written, first_date, last_date | None).
    """
    all_rows = conn.execute(
        "SELECT report_date, raw_date, head_count, avg_weight, avg_price FROM mars_sales ORDER BY report_date"
    ).fetchall()

    # by_day keys off the (possibly weekend-shifted) report_date -- used for
    # BOTH the rolling 7-day window and the same-day snapshot below.
    #
    # An earlier version of this function used raw_date (the true calendar
    # sale date) for the same-day snapshot instead, on the theory that a
    # Saturday-only auction (e.g. Ericson NE) shouldn't leak into the
    # following Monday's "Daily" figure. That was wrong -- confirmed
    # directly against CME's own official daily FTP files (see cme_ftp.py):
    # a Monday file's own DAILY TOTALS line consistently equals that
    # Monday's own rows PLUS the preceding Saturday's, matching CME's stated
    # rule ("Saturday and Sunday sales... as if... occurred on Monday")
    # literally rather than just for the rolling window. raw_date is still
    # tracked and used for per-row display (e.g. the Sale Locations table,
    # cme_ftp_locations) -- CME's own files likewise keep a weekend row's
    # true date visible per-location while still folding its total into the
    # following business day's combined figure.
    by_day = {}  # report_date -> list of (weight_lbs, dollars, head)
    for report_date, raw_date, head, wt, price in all_rows:
        w = head * wt
        by_day.setdefault(report_date, []).append((w, w * price, head))

    all_dates = sorted(date.fromisoformat(d) for d in by_day)
    if not all_dates:
        conn.execute("DELETE FROM fci_daily")
        conn.commit()
        return 0, None, None
    first_date, last_date = all_dates[0], all_dates[-1]
    conn.execute("DELETE FROM fci_daily")

    n_written = 0
    d = first_date
    while d <= last_date:
        window_start = d - timedelta(days=6)
        window_days = [
            (window_start + timedelta(days=i)).isoformat() for i in range(7)
        ]
        den = num = 0.0
        n_locs = 0
        total_head = 0
        for wd in window_days:
            for w, dollars, head in by_day.get(wd, []):
                den += w
                num += dollars
                n_locs += 1
                total_head += head

        # Same-day-only snapshot (not rolling) -- matches the "Daily: $X on Y
        # head and Z lbs average" figure CME's own subscriber reports quote
        # alongside the 7-day index. None when no report landed that date
        # (weekends etc.), same as the report showing no standalone row then.
        sd_den = sd_num = 0.0
        sd_head = 0
        for w, dollars, head in by_day.get(d.isoformat(), []):
            sd_den += w
            sd_num += dollars
            sd_head += head
        sd_price = (sd_num / sd_den) if sd_den > 0 else None
        sd_avg_weight = (sd_den / sd_head) if sd_head > 0 else None

        if den > 0:
            conn.execute(
                "INSERT INTO fci_daily "
                "(report_date, fci_value, n_locations, total_head, same_day_price, same_day_head, same_day_avg_weight) "
                "VALUES (?,?,?,?,?,?,?)",
                (d.isoformat(), num / den, n_locs, total_head, sd_price, sd_head or None, sd_avg_weight),
            )
            n_written += 1
        d += timedelta(days=1)
    conn.commit()
    return n_written, first_date, last_date


def run_update(since: date, verbose=True):
    auth = get_auth()
    roster = json.loads(ROSTER_PATH.read_text(encoding="utf-8"))
    until = date.today()
    since_str, until_str = mdY(since), mdY(until)

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    total_inserted = 0
    for loc in roster:
        slug_id = loc["slug_id"]
        try:
            rows = fetch_slug(slug_id, since_str, until_str, auth)
        except Exception as e:
            if verbose:
                print(f"  [skip] slug {slug_id} ({loc['title']}): {e}")
            continue
        qrows = qualifying_rows(rows)
        for r in qrows:
            rd = r["report_date"]  # MM/DD/YYYY
            m, d, y = rd.split("/")
            sale_date = date(int(y), int(m), int(d))
            iso_date = shift_weekend_to_monday(sale_date).isoformat()
            conn.execute(
                "INSERT OR IGNORE INTO mars_sales "
                "(report_date, raw_date, slug_id, location, state, weight_low, muscle_grade, head_count, avg_weight, avg_price) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (iso_date, sale_date.isoformat(), slug_id, loc["city"] or loc["title"], loc["state"],
                 r["weight_break_low"], r["muscle_grade"],
                 r["head_count"], r["avg_weight"], r["avg_price"]),
            )
        total_inserted += len(qrows)
        if verbose and qrows:
            print(f"  {loc['state']:>2} {loc['city'] or loc['title']:<28} +{len(qrows)} rows")

    # Direct/Video/Internet trade (Direct Cattle Report family). These PDFs
    # always show the CURRENT week only -- there's no historical-date param,
    # so this only extends the dataset forward from whenever it's first run,
    # same limitation the original auction backfill had. Weekly, not daily:
    # every qualifying row gets that week's Friday date (CME's own rule
    # treats direct-trade reports as Friday sales).
    if verbose:
        print("\nDirect trade reports (this week only):")
    direct_results = fetch_all_direct_rows(verbose=verbose)
    direct_inserted = 0
    for state, (report_date_, rows) in direct_results.items():
        if report_date_ is None:
            continue
        iso_date = report_date_.isoformat()
        for r in rows:
            conn.execute(
                "INSERT OR IGNORE INTO mars_sales "
                "(report_date, raw_date, slug_id, location, state, weight_low, muscle_grade, head_count, avg_weight, avg_price) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (iso_date, iso_date, DIRECT_REPORT_SLUGS[state], f"{state} DIRECT", state,
                 r["weight_break_low"], r["muscle_grade"],
                 r["head_count"], r["avg_weight"], r["avg_price"]),
            )
        direct_inserted += len(rows)
    total_inserted += direct_inserted

    # Video/internet auction trade: Superior Livestock (by far the largest
    # platform, ~200k head/week), plus Cattle Country Video, CMS, LiveAg,
    # and Northern Livestock (see video_reports.py for the smaller per-city
    # add-ons checked and skipped as not worth building). Same
    # current-week-only limitation as the direct reports. Rows are
    # attributed to a REGION (North Central /
    # South Central), not a single state -- video sales aren't broken out
    # by state within a region -- so `state` here is the region name
    # itself, not a real 2-letter code; that's intentional, not a bug.
    if verbose:
        print("\nVideo auction reports (this week only):")
    video_results = fetch_all_video_rows(verbose=verbose)
    video_inserted = 0
    for name, (report_date_, rows) in video_results.items():
        if report_date_ is None:
            continue
        iso_date = shift_weekend_to_monday(report_date_).isoformat()
        slug_id = VIDEO_REPORT_SLUGS[name]
        for r in rows:
            conn.execute(
                "INSERT OR IGNORE INTO mars_sales "
                "(report_date, raw_date, slug_id, location, state, weight_low, muscle_grade, head_count, avg_weight, avg_price) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (iso_date, report_date_.isoformat(), slug_id, f"{name} VIDEO ({r['region']})", r["region"],
                 r["weight_break_low"], r["muscle_grade"],
                 r["head_count"], r["avg_weight"], r["avg_price"]),
            )
        video_inserted += len(rows)
    total_inserted += video_inserted
    conn.commit()

    n_written, first_date, last_date = recompute_fci_daily(conn)

    if verbose:
        print(f"\nInserted/kept {total_inserted} sale rows: {total_inserted - direct_inserted - video_inserted} "
              f"auction rows across {len(roster)} locations, {direct_inserted} direct-trade rows across "
              f"{len(direct_results)} states, {video_inserted} video-auction rows across {len(video_results)} reports.")
        print(f"Recomputed FCI (7-day rolling window) for {n_written} dates "
              f"({first_date or '—'} to {last_date or '—'}).")
        recent = conn.execute(
            "SELECT report_date, fci_value, n_locations FROM fci_daily ORDER BY report_date DESC LIMIT 8"
        ).fetchall()
        print("\nMost recent reconstructed index values:")
        for d, v, n in recent:
            print(f"  {d}  ${v:.2f}   ({n} locations)")
    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", type=str, default=None,
                         help="ISO date to pull from (default: continue from last stored date, "
                              "or 2026-01-24 on first run)")
    args = parser.parse_args()

    if args.since:
        since = date.fromisoformat(args.since)
    elif DB_PATH.exists():
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT MAX(report_date) FROM fci_daily").fetchone()
        conn.close()
        since = date.fromisoformat(row[0]) - timedelta(days=7) if row and row[0] else CONTINUATION_START
    else:
        since = CONTINUATION_START

    print(f"Updating CME Feeder Cattle Index reconstruction since {since.isoformat()}...\n")
    run_update(since)
