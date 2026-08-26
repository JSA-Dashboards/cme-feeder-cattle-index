"""
Extends the CME Feeder Cattle Index dashboard's history past where the Ross
workbook stops (2026-01-23) using USDA AMS MARS API data.

Methodology mirrors the workbook's own 'FCI Estimation' sheet: for each
report date, pull every qualifying sale row (Steers, Medium & Large frame,
grade #1 or #1-2, 700-899 lb weight brackets) from a fixed roster of ~60
sale-barn reports across the CME 12-state region, then compute

    FCI(date) = sum(head_count * avg_weight * avg_price) / sum(head_count * avg_weight)

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
            total_head INTEGER NOT NULL
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

    # Recompute daily FCI from scratch over the affected window (idempotent).
    cur = conn.execute(
        "SELECT report_date, head_count, avg_weight, avg_price FROM mars_sales WHERE report_date >= ?",
        (since.isoformat(),),
    )
    by_date = {}
    for report_date, head, wt, price in cur.fetchall():
        num, den, n = by_date.get(report_date, (0.0, 0.0, 0))
        w = head * wt
        by_date[report_date] = (num + w * price, den + w, n + 1)

    for report_date, (num, den, n) in by_date.items():
        if den <= 0:
            continue
        fci = num / den
        total_head = conn.execute(
            "SELECT COALESCE(SUM(head_count),0) FROM mars_sales WHERE report_date = ?", (report_date,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO fci_daily (report_date, fci_value, n_locations, total_head) VALUES (?,?,?,?) "
            "ON CONFLICT(report_date) DO UPDATE SET fci_value=excluded.fci_value, "
            "n_locations=excluded.n_locations, total_head=excluded.total_head",
            (report_date, fci, n, total_head),
        )
    conn.commit()

    if verbose:
        print(f"\nInserted/kept {total_inserted} sale rows across {len(roster)} locations.")
        print(f"Recomputed FCI for {len(by_date)} dates since {since.isoformat()}.")
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
