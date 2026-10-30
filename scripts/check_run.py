"""
The data half of scripts/check_run.ps1: what the run actually produced.

Reads SNOWFLAKE, not the local SQLite, because that is what the dashboard
serves. A run can succeed locally and fail to publish -- daily_update.ps1 has
exit code 5 for exactly that -- and checking the local file would report
success while the page stayed stale.

Read-only.
"""
import os
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
os.environ["USE_SNOWFLAKE"] = "1"          # check what the dashboard sees

import cme_ftp  # noqa: E402
import snowflake_db as db  # noqa: E402

run_date = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
# The index date a morning run is estimating is the day before it runs.
index_date = (date.fromisoformat(run_date) - timedelta(days=1)).isoformat()

conn = db.get_conn()
cur = conn.cursor()
ph = db.placeholders(1)


def one(sql, args=()):
    r = cur.execute(sql, args).fetchone()
    return r if r else None


print(f"   backend: {'snowflake' if db.use_snowflake() else 'sqlite'}  "
      f"(the dashboard reads this one)")

# --- did the run freeze a morning call? -------------------------------------
rows = cur.execute(
    f"SELECT run_slot, MAX(captured_at), COUNT(*) FROM fci_snapshots "
    f"WHERE run_date = {ph} GROUP BY run_slot ORDER BY run_slot", (run_date,)
).fetchall()
if not rows:
    print(f"   NO snapshots for run_date {run_date} -- the run did not reach "
          f"capture_snapshots(), or did not publish")
else:
    for slot, last, n in rows:
        print(f"   {slot} slot: {n} dates frozen, at {db.iso(last)}")

# --- freshness, the same calculation the dashboard banner makes --------------
mx = one("SELECT MAX(captured_at) FROM fci_snapshots")
if mx and mx[0]:
    last = datetime.fromisoformat(str(db.iso(mx[0])))
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/Chicago")).replace(tzinfo=None)
        hrs = (now - last).total_seconds() / 3600
        state = ("ALARM" if hrs >= 30 else "WARNING" if hrs >= 20
                 else "future-dated" if hrs < -0.5 else "healthy")
        print(f"   dashboard freshness banner would read: {state} ({hrs:.1f}h)")
    except Exception:
        print(f"   last refresh {last} (no tz database, age not computed)")

# --- the call, and CME's verdict on it --------------------------------------
print(f"\n   index date being estimated: {index_date}")
frozen = one(f"SELECT fci_value, total_head FROM fci_snapshots WHERE index_date = {ph} "
             f"AND run_slot = 'am' ORDER BY captured_at LIMIT 1", (index_date,))
live = one(f"SELECT fci_value, total_head FROM fci_daily WHERE report_date = {ph}",
           (index_date,))
cme = one(f"SELECT fci_value, total_head FROM cme_ftp_daily WHERE report_date = {ph}",
          (index_date,))

if frozen:
    print(f"   JSA frozen morning call  ${frozen[0]:.4f} on {int(frozen[1]):,} head")
if live and (not frozen or abs(live[0] - frozen[0]) > 1e-9):
    print(f"   JSA current (revised)    ${live[0]:.4f} on {int(live[1]):,} head")
if cme:
    print(f"   CME published            ${cme[0]:.2f} on {int(cme[1]):,} head")
    if frozen:
        head_note = " -- IDENTICAL window" if int(frozen[1]) == int(cme[1]) else ""
        print(f"   -> our miss {frozen[0] - cme[0]:+.4f}{head_note}")
else:
    # THE BAND IS 13:04-15:05 CENTRAL, and this line said 08:05-10:05 until
    # 2026-10-22 -- the same five-hour error CLAUDE.md records as fixed,
    # surviving in a second file because the fix was applied where it was
    # found rather than where it was repeated. It also named the 13:00 run as
    # the collector, which loses the race by minutes; the 14:15 pull exists
    # for precisely this.
    #
    # THE CEILING SAID 14:04 UNTIL 2026-10-30, which was that same error only
    # half corrected: the old worst case 10:05 plus five is 15:05, and 14:04
    # is the old MEDIAN plus five. Re-measured 2026-10-30 off 159 business-day index dates: min 08:05, p10 13:04, median 13:35, p90 15:05, p99 16:04, max 20:48.
    print("   CME has not published this index date yet (posts 13:04-15:05 "
          "Central, so the 14:15 pull catches most but not all of them)")

peers = cur.execute(
    f"SELECT source, fci_value FROM peer_estimates WHERE index_date = {ph} "
    f"ORDER BY source", (index_date,)).fetchall()
if peers:
    for src, val in peers:
        miss = f"  miss {val - cme[0]:+.4f}" if cme else "  (unscored)"
        print(f"   {src:<22} ${val:.4f}{miss}")
else:
    print("   no peer estimates recorded for this date yet "
          "(add_peer_estimate.py --date %s --source CIH --value ...)" % index_date)

# --- did anything land that the morning run could not have seen? ------------
newest = one("SELECT MAX(report_date) FROM cme_ftp_daily")
if newest and newest[0]:
    # A DATE WITH A VERDICT, not a date on its own. This printed the series
    # end as bare information, which left the reader to know CME's publication
    # clock by heart and do the business-day arithmetic at 07:00 -- so a
    # missing print read exactly like a current one. On 2026-10-21 the series
    # ended 10-19 with 10-20 already owed, and nothing anywhere said so.
    end = db.iso(newest[0])
    status = cme_ftp.publication_status(date.fromisoformat(end), datetime.now())
    if status["overdue"]:
        print(f"\n   CME series runs through {end} -- BEHIND: "
              f"{status['due_through']} should be published by now "
              f"({len(status['overdue'])} business day(s) owed). Nothing on "
              f"our side failed; there is no file to fetch.")
    else:
        print(f"\n   CME series runs through {end} -- current")
conn.close()
