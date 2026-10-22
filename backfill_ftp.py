"""
One-off (re-runnable) backfill: pulls CME's own official daily Feeder Cattle
Index files (see cme_ftp.py) for a date range and stores them in
cme_ftp_daily / cme_ftp_locations. Idempotent -- INSERT OR REPLACE per date,
safe to re-run or resume.

    python backfill_ftp.py --catch-up                     # what a scheduled run does
    python backfill_ftp.py --start 2015-01-01 --end 2026-08-31
    python backfill_ftp.py --start 2015-01-01 --only-missing   # fill historical holes

--catch-up ASKS WHAT IS MISSING instead of re-fetching a fixed window. The
window was four days in the 14:15 job and ten in the daily one, which made a
long CME outage unrecoverable: when CME came back, every date older than the
window had aged out and was never asked for again. Because the catch-up fetches
only dates it does not already hold, reaching back ninety days costs nothing on
a morning when nothing is missing.
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
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--catch-up", action="store_true",
                    help="fetch every business day we do not hold, back to the "
                         "catch-up horizon. What the scheduled jobs run.")
    ap.add_argument("--only-missing", action="store_true",
                    help="with --start: skip dates already held, and weekends")
    args = ap.parse_args()
    if not args.start and not args.catch_up:
        ap.error("one of --start or --catch-up is required")

    now = datetime.now()

    conn = db.get_conn()
    if not db.use_snowflake():
        # WAL mode lets Streamlit keep reading the DB while this long-running
        # backfill writes to it -- without it, sqlite's default
        # rollback-journal locking blocks reads for the entire run once any
        # write is pending.
        conn.execute("PRAGMA journal_mode=WAL")
    cme_ftp.init_official_tables(conn)

    def already_held():
        cur0 = conn.cursor()
        return {str(db.iso(r[0]))[:10] for r in
                cur0.execute("SELECT report_date FROM cme_ftp_daily")}

    if args.catch_up:
        # THE GAP, NOT A WINDOW. See cme_ftp.catch_up_dates: business days we
        # do not hold, up to the newest date CME's own clock says should
        # exist. An outage of any length inside the horizon stays in scope for
        # as long as it is still a gap, instead of ageing out after four days.
        dates = cme_ftp.catch_up_dates(already_held(), now)
        span = f"the {cme_ftp.CATCHUP_HORIZON_DAYS}-day catch-up horizon"
    else:
        start = datetime.strptime(args.start, "%Y-%m-%d").date()
        end = (datetime.strptime(args.end, "%Y-%m-%d").date() if args.end
               else date.today())
        dates = list(cme_ftp.daterange(start, end))
        span = f"{start} to {end}"
        if args.only_missing:
            held = already_held()
            dates = [d for d in dates
                     if d.weekday() < 5 and d.isoformat() not in held]
            span += " (business days not already held)"

    if dates:
        print(f"Fetching {len(dates)} date(s) over {span}, serially with a "
              f"{REQUEST_DELAY_S}s pause between requests...")
    else:
        print(f"Nothing to fetch over {span} -- every business day up to CME's "
              f"own due date is already held.")

    t0 = time.time()
    results = {}
    failed = {}
    for i, d in enumerate(dates):
        try:
            results[d] = cme_ftp.fetch_daily_file(d)
        except cme_ftp.FetchFailed as e:
            results[d] = None
            failed[d] = str(e)
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
            try:
                results[d] = cme_ftp.fetch_daily_file(d)
                failed.pop(d, None)
            except cme_ftp.FetchFailed as e:
                results[d] = None
                failed[d] = str(e)
            time.sleep(REQUEST_DELAY_S)

    ingested = absent = 0
    unreadable = []
    for d in dates:
        text = results.get(d)
        if text is None:
            if d not in failed:
                absent += 1        # server says the path is not there
            continue               # fetch failures counted separately
        parsed = cme_ftp.parse_daily_file(text, d)
        if parsed is None or parsed["reported_index"] is None:
            unreadable.append(d)   # we HAVE the file and cannot read it
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
        # Two independent derivations of the same row, compared. Empty every
        # ordinary day; see cme_ftp.bracket_anomalies for why this is
        # arithmetic rather than a token count.
        for bad in cme_ftp.bracket_anomalies(parsed):
            print("  *** CME BRACKETS DO NOT SUM TO THE ROW: {} {} -- "
                  "row states {:,} head / {:,.0f} lb, its brackets give "
                  "{:,} head / {:,.0f} lb. The file's layout may have changed; "
                  "do not trust this row's brackets.".format(
                      bad["date"], bad["location"], bad["stated_head"],
                      bad["stated_lb"] or 0, bad["bracket_head"],
                      bad["bracket_lb"]))
        for loc in parsed["locations"]:
            for b in loc.get("brackets") or []:
                db.merge_replace(
                    conn, "cme_ftp_brackets",
                    ["report_date", "raw_date", "location", "state", "grade",
                     "weight_low", "head_count", "avg_weight", "avg_price"],
                    (parsed["date"], loc["raw_date"], loc["location"], loc["state"],
                     b["grade"], b["weight_low"], b["head"], b["avg_weight"],
                     b["avg_price"]),
                    ["report_date", "location", "grade", "weight_low"],
                )
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
    print(f"\nDone in {elapsed:.0f}s. Ingested {ingested} days; "
          f"{absent} had no file (weekend/holiday/not yet published).")
    # These two used to be folded into the "no file" count, which turned a
    # transient block or a parser gap into an apparent statement about what
    # CME had published. Both are OUR problem, and both are now loud.
    if failed:
        print(f"  !! {len(failed)} date(s) FAILED to fetch -- NOT the same "
              f"as unpublished:")
        for d in sorted(failed)[:10]:
            print(f"       {d}: {failed[d]}")
    if unreadable:
        print(f"  !! {len(unreadable)} file(s) fetched but UNREADABLE -- we "
              f"hold the data and cannot parse it:")
        for d in sorted(unreadable)[:10]:
            print(f"       {d}")
    print(f"cme_ftp_daily now has {n_rows} total rows, spanning {first_last[0]} to {first_last[1]}.")

    # ONE MACHINE-READABLE LINE, for scripts/cme_pull.ps1 to judge rather than
    # assume. Everything above is prose for a human; this is the only line a
    # caller parses, and it exists so the word "current" in that job's log can
    # be gated on a fact instead of on a push exit code.
    #
    # Printed on EVERY run, including failures, so a missing CME_STATUS means
    # "this ran an older backfill_ftp.py" rather than "all clear" -- cme_pull
    # treats its absence as a warning for exactly that reason.
    end_date = date.fromisoformat(first_last[1]) if first_last[1] else None
    status = cme_ftp.publication_status(end_date, now)
    due = status["due_through"]
    print("CME_STATUS series_end={} due_through={} overdue_days={} "
          "fetch_failures={} unreadable={}".format(
              first_last[1] or "none",
              due.isoformat() if due else "none",
              len(status["overdue"]), len(failed), len(unreadable)))
    if status["overdue"]:
        # Said plainly as well, because the status line is for a parser and
        # this log is read by a person. NOT an error: CME being late is not
        # our failure, and the exit code deliberately does not move for it.
        print("  !! CME IS BEHIND: we hold through {}, and {} should have been "
              "published by now ({} business day(s) owed: {}). Nothing on our "
              "side failed -- there is no file to fetch.".format(
                  first_last[1], due.isoformat(), len(status["overdue"]),
                  ", ".join(d.isoformat() for d in status["overdue"][:5])))

    # EXIT NON-ZERO WHEN WE HOLD FILES WE CANNOT READ. This printed a warning
    # and exited 0 until 2026-09-17, which is how a CME layout change on 09-14
    # went unnoticed for three days: the daily job logged cme_exit=0, the
    # healthcheck stayed green, the dashboard kept serving a headline frozen at
    # 09/14, and the only sign was a line in a log nobody greps.
    #
    # "Fetched but unparseable" is a genuine error -- distinct from "no file",
    # which is a normal weekend or a print that has not landed yet. The daily
    # job treats a non-zero CME exit as non-fatal and logs a WARN, which is the
    # right severity: the index estimate does not depend on this, but somebody
    # needs to know the published series has stopped moving.
    return 1 if unreadable else 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
