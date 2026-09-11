"""
Daily refresh for the Mexican feeder import sources. Called by
scripts/daily_update.ps1 before the Snowflake push.

TWO SOURCES ON DELIBERATELY DIFFERENT LOOKBACKS, because they revise on
completely different schedules:

  AMS border reports    a short lookback is enough -- these publish same-day and
                        are not revised. 30 days covers any report that landed
                        late or a stretch of missed runs.

  Census trade data     a LONG lookback is mandatory, not generous. Census
                        revises prior months for months afterwards, and its
                        first release of a month is provisional. A 3-month
                        window would freeze the first, wrong print of anything
                        older. 15 months re-pulls the whole current year plus
                        the prior one on every run, which is ~30 requests and
                        costs seconds.

Both are non-fatal by design. Neither feeds the FCI estimate, so a border-report
outage must never stop the index pipeline from publishing -- the worst case is
one stale dashboard tab. Exit code is always 0 unless BOTH sources fail, which
is the signature of a broken credential or no network rather than a bad day at
one agency.
"""
import sys
import traceback
from datetime import date, timedelta

from dotenv import load_dotenv

load_dotenv()

import snowflake_db as db

AMS_LOOKBACK_DAYS = 30
CENSUS_LOOKBACK_MONTHS = 15


def _months_ago(n: int) -> str:
    d = date.today()
    y, m = d.year, d.month - n
    while m <= 0:
        y, m = y - 1, m + 12
    return f"{y:04d}-{m:02d}"


def main() -> int:
    conn = db.get_conn()
    ok = []

    try:
        import border_reports
        border_reports.init_tables(conn)
        since = date.today() - timedelta(days=AMS_LOOKBACK_DAYS)
        print(f"--- AMS border reports since {since} ---")
        n = border_reports.ingest(conn, since, date.today())
        print(f"    {n:,} rows")
        st = border_reports.current_status(conn)
        if st and st.get("notes"):
            print(f"    status: {' '.join(str(st['notes']).split())[:120]}")
        ok.append("ams")
    except Exception:
        print("[!] AMS border reports FAILED:")
        traceback.print_exc(file=sys.stdout)

    try:
        import census_imports
        census_imports.init_tables(conn)
        start = _months_ago(CENSUS_LOOKBACK_MONTHS)
        end = date.today().strftime("%Y-%m")
        print(f"--- Census live cattle imports {start} .. {end} ---")
        # ports=False: the port-level endpoint doubles the request count and the
        # dashboard does not read it yet. Turn it on when the page grows a
        # by-port volume view -- the table already has the columns.
        n = census_imports.ingest(conn, start, end, ports=False, verbose=True)
        print(f"    {n:,} rows")
        ok.append("census")
    except Exception:
        print("[!] Census imports FAILED:")
        traceback.print_exc(file=sys.stdout)

    conn.close()
    if not ok:
        print("both import sources failed - check MARS_API_KEY / CENSUS_API_KEY")
        return 1
    print(f"import refresh OK ({', '.join(ok)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
