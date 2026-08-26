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
CME's real index shows. Note: this still excludes the Direct/Video/
Internet trade component of CME's sample (see reference_usda_mars_api
memory — that data isn't available as structured rows via this API).

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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mars_sales (
            report_date TEXT NOT NULL,
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
            iso_date = f"{y}-{int(m):02d}-{int(d):02d}"
            conn.execute(
                "INSERT OR IGNORE INTO mars_sales "
                "(report_date, slug_id, location, state, weight_low, muscle_grade, head_count, avg_weight, avg_price) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (iso_date, slug_id, loc["city"] or loc["title"], loc["state"],
                 r["weight_break_low"], r["muscle_grade"],
                 r["head_count"], r["avg_weight"], r["avg_price"]),
            )
        total_inserted += len(qrows)
        if verbose and qrows:
            print(f"  {loc['state']:>2} {loc['city'] or loc['title']:<28} +{len(qrows)} rows")
    conn.commit()

    # Recompute the FULL fci_daily table from ALL stored mars_sales, using a
    # rolling 7-calendar-day trailing window per CME's published methodology
    # (each date's window can reach up to 6 days before `since`, so this is
    # NOT limited to the affected date range — it's cheap given table size).
    all_rows = conn.execute(
        "SELECT report_date, head_count, avg_weight, avg_price FROM mars_sales ORDER BY report_date"
    ).fetchall()

    by_day = {}  # report_date -> list of (weight_lbs, dollars, head)
    for report_date, head, wt, price in all_rows:
        w = head * wt
        by_day.setdefault(report_date, []).append((w, w * price, head))

    all_dates = sorted(date.fromisoformat(d) for d in by_day)
    if all_dates:
        first_date, last_date = all_dates[0], all_dates[-1]
    conn.execute("DELETE FROM fci_daily")

    n_written = 0
    d = first_date if all_dates else None
    while d is not None and d <= last_date:
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

    if verbose:
        print(f"\nInserted/kept {total_inserted} sale rows across {len(roster)} locations.")
        print(f"Recomputed FCI (7-day rolling window) for {n_written} dates "
              f"({first_date if all_dates else '—'} to {last_date if all_dates else '—'}).")
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
