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

class _Skip(Exception):
    """Raised inside a source block that --only excluded. Caught before the
    generic handler, so a skip is never reported as a failure."""


AMS_LOOKBACK_DAYS = 30
CENSUS_LOOKBACK_MONTHS = 15


def _months_ago(n: int) -> str:
    d = date.today()
    y, m = d.year, d.month - n
    while m <= 0:
        y, m = y - 1, m + 12
    return f"{y:04d}-{m:02d}"


SOURCES = ("ams", "calf", "corn", "feed", "census")


def main(argv=None) -> int:
    """
    --only ams[,calf,...] runs a subset. Everything by default, so the
    desktop's call is unchanged.

    EXISTS SO THE DROPLET CAN TAKE ONE SOURCE WITHOUT TAKING ALL FIVE.
    deploy/run_imports.sh runs `--only ams`: AMS publishes the border report
    the same day and the desktop's 07:45/13:00 triggers caught 0 and 1 of 2026's
    47, so the crossings were a day behind by construction. The other four have
    the same shape and the same fix available, and widening that job is one word
    -- but a flag that silently accepted an unknown name would quietly run
    NOTHING and still exit 0, which is the failure this whole script is about.
    So an unrecognised source is fatal before any work starts.
    """
    only = None
    argv = sys.argv[1:] if argv is None else list(argv)
    if "--only" in argv:
        i = argv.index("--only")
        if i + 1 >= len(argv):
            print("--only needs a comma-separated list of: " + ", ".join(SOURCES))
            return 2
        only = {w.strip().lower() for w in argv[i + 1].split(",") if w.strip()}
        unknown = only - set(SOURCES)
        if unknown:
            print(f"--only: unknown source(s) {sorted(unknown)}; "
                  f"known are {', '.join(SOURCES)}")
            return 2

    def wanted(name: str) -> bool:
        return only is None or name in only

    conn = db.get_conn()
    ok = []
    skipped = [s for s in SOURCES if not wanted(s)]
    if skipped:
        print(f"--only {','.join(sorted(only))} — skipping {', '.join(skipped)}")

    try:
        if not wanted("ams"):
            raise _Skip()
        import border_reports
        border_reports.init_tables(conn)
        since = date.today() - timedelta(days=AMS_LOOKBACK_DAYS)
        print(f"--- AMS border reports since {since} ---")
        n = border_reports.ingest(conn, since, date.today())
        border_reports.init_receipt_tables(conn)
        n += border_reports.ingest_receipts(conn, since, date.today())
        # 3629 carries AMS's running YTD, so a short window would only ever
        # refresh the latest week's figure -- fine, since the YTD is restated
        # in full on every week's row.
        n += border_reports.ingest_volumes(conn, since, date.today())
        n += border_reports.ingest_prices(conn, since, date.today())
        print(f"    {n:,} rows")
        st = border_reports.current_status(conn)
        if st and st.get("notes"):
            print(f"    status: {' '.join(str(st['notes']).split())[:120]}")
        ok.append("ams")
    except _Skip:
        pass
    except Exception:
        print("[!] AMS border reports FAILED:")
        traceback.print_exc(file=sys.stdout)

    try:
        if not wanted("calf"):
            raise _Skip()
        import calf_sales
        calf_sales.init_tables(conn)
        since = date.today() - timedelta(days=AMS_LOOKBACK_DAYS)
        print(f"--- calf/feeder auction rows since {since} ---")
        n = calf_sales.ingest(conn, since, date.today(), verbose=False)
        print(f"    {n:,} rows")
        ok.append("calf")
    except _Skip:
        pass
    except Exception:
        print("[!] calf sales FAILED:")
        traceback.print_exc(file=sys.stdout)

    try:
        if not wanted("corn"):
            raise _Skip()
        import corn_bids
        corn_bids.init_tables(conn)
        since = date.today() - timedelta(days=AMS_LOOKBACK_DAYS)
        print(f"--- cash corn bids since {since} ---")
        n = corn_bids.ingest(conn, since, date.today(), verbose=False)
        print(f"    {n:,} rows")
        ok.append("corn")
    except _Skip:
        pass
    except Exception:
        print("[!] corn bids FAILED:")
        traceback.print_exc(file=sys.stdout)

    try:
        if not wanted("feed"):
            raise _Skip()
        import feed_bids
        feed_bids.init_tables(conn)
        # 90 days, not 30: Direct Hay is a period report published roughly
        # weekly or biweekly, and the co-products report is weekly. A 30-day
        # window like the auction feeds use would catch only a handful of
        # prints per state, and a missed run could leave a state with none.
        since = date.today() - timedelta(days=90)
        print(f"--- feed prices (distillers, hay) since {since} ---")
        n = feed_bids.ingest_distillers(conn, since, date.today(), verbose=False)
        n += feed_bids.ingest_hay(conn, since, date.today(), verbose=False)
        print(f"    {n:,} rows")
        ok.append("feed")
    except _Skip:
        pass
    except Exception:
        print("[!] feed prices FAILED:")
        traceback.print_exc(file=sys.stdout)

    try:
        if not wanted("census"):
            raise _Skip()
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
    except _Skip:
        pass
    except Exception:
        print("[!] Census imports FAILED:")
        traceback.print_exc(file=sys.stdout)

    conn.close()
    if not ok:
        # NOT "both sources failed" -- with --only there may have been one.
        asked = ", ".join(sorted(only)) if only else ", ".join(SOURCES)
        print(f"every requested source failed ({asked}) "
              f"- check MARS_API_KEY / CENSUS_API_KEY")
        return 1
    print(f"import refresh OK ({', '.join(ok)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
