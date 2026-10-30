"""
Load CME's yearly index workbooks into the ARCHIVE tables. Re-runnable.

    uv run --with xlrd python backfill_cme_archive.py --years 2013 2014
    uv run --with xlrd python backfill_cme_archive.py --all
    uv run --with xlrd python backfill_cme_archive.py --years 2015 --check

xlrd is not a pipeline dependency and is deliberately absent from
requirements.txt -- nothing in the daily job reads a workbook. Hence `uv run
--with`, which builds a throwaway environment rather than changing the venv
the pipeline uses.

THESE TABLES ARE NOT THE SCORECARD, and the column names are chosen so no
query can pretend otherwise. `cme_ftp_daily.fci_value` is the REPORTED INDEX
CME printed on the day; `cme_archive_daily.seven_day_wtd_price` is a weighted
average CME recomputed later, and on 2015 the two differ by up to 0.5382 on
dates whose head counts agree exactly. See cme_archive.py's docstring.

--check parses and reports WITHOUT writing, and on a year we already hold
as-printed it also prints the disagreement, which is the measurement that
justifies keeping the two apart.
"""

import argparse
import sqlite3
from datetime import date
from pathlib import Path

import cme_archive

DB_PATH = Path(__file__).parent / "data" / "mars_history.db"

#: 2013 is the earliest workbook CME publishes; 2026 the current year.
FIRST_YEAR, LAST_YEAR = 2013, date.today().year

DAILY_DDL = """
CREATE TABLE IF NOT EXISTS cme_archive_daily (
    report_date           TEXT PRIMARY KEY,
    daily_head            REAL,
    daily_weight          REAL,
    daily_wtd_price       REAL,
    seven_day_head        REAL,
    seven_day_weight      REAL,
    seven_day_wtd_price   REAL,
    seven_day_wtd_weight  REAL,
    source_year           INTEGER NOT NULL
)"""

LOC_DDL = """
CREATE TABLE IF NOT EXISTS cme_archive_locations (
    report_date  TEXT NOT NULL,
    location     TEXT NOT NULL,
    state        TEXT,
    head         REAL,
    weight       REAL,
    wtd_price    REAL,
    wtd_weight   REAL,
    source_year  INTEGER NOT NULL,
    PRIMARY KEY (report_date, location)
)"""


def ensure(conn):
    conn.execute(DAILY_DDL)
    conn.execute(LOC_DDL)
    conn.commit()


def compare_against_as_printed(conn, daily):
    """On a year we already hold as-printed, how far apart are the two?

    Printed rather than asserted: the point is not that they match -- they do
    not -- but that anyone running this sees the size of the gap before
    deciding what the archive is good for.
    """
    held = {}
    try:
        for d, v, h in conn.execute(
                "SELECT report_date, fci_value, total_head FROM cme_ftp_daily"):
            held[str(d)[:10]] = (v, h)
    except sqlite3.OperationalError:
        return
    both = [r for r in daily if r["report_date"].isoformat() in held]
    if not both:
        return
    worst = 0.0
    head_off = 0
    for r in both:
        hv, hh = held[r["report_date"].isoformat()]
        if hv is not None and r.get("seven_day_wtd_price") is not None:
            worst = max(worst, abs(r["seven_day_wtd_price"] - hv))
        if hh is not None and r.get("seven_day_head") is not None:
            head_off += abs(r["seven_day_head"] - hh) > 0.5
    print("      vs as-printed on %d shared dates: worst price gap %.4f, "
          "%d head mismatches" % (len(both), worst, head_off))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", nargs="+", type=int)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--check", action="store_true",
                    help="parse and report, write nothing")
    args = ap.parse_args()

    if args.all:
        years = list(range(FIRST_YEAR, LAST_YEAR + 1))
    elif args.years:
        years = args.years
    else:
        ap.error("pass --years or --all")

    conn = sqlite3.connect(DB_PATH)
    if not args.check:
        ensure(conn)

    for year in years:
        print("FC%d" % year)
        try:
            raw = cme_archive.fetch(year)
            daily, locs = cme_archive.parse(cme_archive.inflate_only_member(raw))
        except cme_archive.ArchiveError as e:
            print("   SKIPPED: %s" % e)
            continue
        if not daily:
            print("   SKIPPED: no dated aggregate rows found")
            continue
        print("   %d dates %s..%s, %d location rows"
              % (len(daily), daily[0]["report_date"], daily[-1]["report_date"],
                 len(locs)))
        # A workbook legitimately carries the previous year's last trading day
        # (FC2015 opens on 2014-12-31). A date MONTHS outside its own year is
        # something else: FC2018 carries 2019-04-01 and FC2022 carries
        # 2023-04-03, which are CME's errors, not ours. Flagged rather than
        # dropped -- source_year records where each row came from, and running
        # the years in order lets the date's OWN workbook overwrite a stray.
        stray = [r["report_date"] for r in daily
                 if abs((r["report_date"] - date(year, 7, 1)).days) > 200]
        if stray:
            print("      NOTE: %d date(s) well outside FC%d: %s"
                  % (len(stray), year, ", ".join(str(s) for s in stray[:4])))
        compare_against_as_printed(conn, daily)
        if args.check:
            continue
        conn.executemany(
            "INSERT OR REPLACE INTO cme_archive_daily (report_date, daily_head,"
            " daily_weight, daily_wtd_price, seven_day_head, seven_day_weight,"
            " seven_day_wtd_price, seven_day_wtd_weight, source_year)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            [(r["report_date"].isoformat(), r.get("daily_head"),
              r.get("daily_weight"), r.get("daily_wtd_price"),
              r.get("seven_day_head"), r.get("seven_day_weight"),
              r.get("seven_day_wtd_price"), r.get("seven_day_wtd_weight"), year)
             for r in daily])
        conn.executemany(
            "INSERT OR REPLACE INTO cme_archive_locations (report_date,"
            " location, state, head, weight, wtd_price, wtd_weight, source_year)"
            " VALUES (?,?,?,?,?,?,?,?)",
            [(r["report_date"].isoformat(), r["location"], r["state"],
              r["head"], r["weight"], r["wtd_price"], r["wtd_weight"], year)
             for r in locs])
        conn.commit()
        print("   stored")

    if not args.check:
        n = conn.execute("SELECT COUNT(*) FROM cme_archive_daily").fetchone()[0]
        lo, hi = conn.execute(
            "SELECT MIN(report_date), MAX(report_date) FROM cme_archive_daily"
        ).fetchone()
        m = conn.execute("SELECT COUNT(*) FROM cme_archive_locations").fetchone()[0]
        print("\ncme_archive_daily: %d rows, %s .. %s" % (n, lo, hi))
        print("cme_archive_locations: %d rows" % m)
    conn.close()


if __name__ == "__main__":
    main()
