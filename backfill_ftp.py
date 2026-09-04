"""
One-off (re-runnable) backfill: pulls CME's own official daily Feeder Cattle
Index files (see cme_ftp.py) for a date range and stores them in
cme_ftp_daily / cme_ftp_locations. Idempotent -- INSERT OR REPLACE per date,
safe to re-run or resume.

    python backfill_ftp.py --start 2015-01-01 --end 2026-08-31
"""
import argparse
import time
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import cme_ftp
import snowflake_db as db

DB_PATH = Path(__file__).parent / "data" / "mars_history.db"
# Confirmed empirically: a burst of concurrent connections trips a temporary
# block (server-side or somewhere in the network path) that takes minutes,
# not seconds, to clear -- and it doesn't matter whether the retry attempts
# are themselves concurrent or serial once tripped. Fully serial with a
# small per-request pause avoids ever triggering it, rather than trying to
# recover after the fact.
REQUEST_DELAY_S = 0.4


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", default=None)
    args = ap.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = datetime.strptime(args.end, "%Y-%m-%d").date() if args.end else date.today()

    conn = db.get_conn()
    if not db.use_snowflake():
        # WAL mode lets Streamlit keep reading the DB while this long-running
        # backfill writes to it -- without it, sqlite's default
        # rollback-journal locking blocks reads for the entire run once any
        # write is pending.
        conn.execute("PRAGMA journal_mode=WAL")
    cme_ftp.init_official_tables(conn)

    dates = list(cme_ftp.daterange(start, end))
    print(f"Fetching {len(dates)} candidate dates ({start} to {end}), serially with a "
          f"{REQUEST_DELAY_S}s pause between requests...")

    t0 = time.time()
    results = {}
    for i, d in enumerate(dates):
        results[d] = cme_ftp.fetch_daily_file(d)
        time.sleep(REQUEST_DELAY_S)
        if (i + 1) % 100 == 0:
            print(f"  ...fetched {i+1}/{len(dates)} ({time.time()-t0:.0f}s elapsed)")

    # A handful of genuine misses can still slip through (a transient hiccup
    # unrelated to the connection-burst issue) -- one gentle retry pass,
    # paced the same way, closes those out without re-risking a burst.
    retry_dates = [d for d in dates if results.get(d) is None]
    if retry_dates:
        print(f"  retry pass: {len(retry_dates)} dates came back empty, re-fetching once more...")
        for d in retry_dates:
            results[d] = cme_ftp.fetch_daily_file(d)
            time.sleep(REQUEST_DELAY_S)

    ingested = missing = 0
    for d in dates:
        text = results.get(d)
        if text is None:
            missing += 1
            continue
        parsed = cme_ftp.parse_daily_file(text, d)
        if parsed is None or parsed["reported_index"] is None:
            missing += 1
            continue
        daily = parsed["daily"] or {}
        seven = parsed["seven_day"] or {}
        db.merge_replace(
            conn, "cme_ftp_daily",
            ["report_date", "fci_value", "reported_change", "n_locations", "total_head",
             "same_day_price", "same_day_head", "same_day_avg_weight"],
            (parsed["date"], parsed["reported_index"], parsed["reported_change"],
             len(parsed["locations"]), seven.get("head"),
             daily.get("avg_price"), daily.get("head"), daily.get("avg_weight")),
            ["report_date"],
        )
        for loc in parsed["locations"]:
            db.merge_replace(
                conn, "cme_ftp_locations",
                ["report_date", "location", "state", "head_count", "avg_weight", "avg_price"],
                (loc["raw_date"], loc["location"], loc["state"], loc["head"],
                 loc["avg_weight"], loc["avg_price"]),
                ["report_date", "location"],
            )
        ingested += 1
        if ingested % 50 == 0:
            conn.commit()

    conn.commit()
    cur = conn.cursor()
    n_rows = cur.execute("SELECT COUNT(*) FROM cme_ftp_daily").fetchone()[0]
    first_last = cur.execute("SELECT MIN(report_date), MAX(report_date) FROM cme_ftp_daily").fetchone()
    first_last = (db.iso(first_last[0]), db.iso(first_last[1]))
    conn.close()

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.0f}s. Ingested {ingested} days, {missing} had no file (weekend/holiday/unpublished).")
    print(f"cme_ftp_daily now has {n_rows} total rows, spanning {first_last[0]} to {first_last[1]}.")


if __name__ == "__main__":
    main()
