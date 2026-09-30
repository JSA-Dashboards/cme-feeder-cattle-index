"""
The AMS reconciliation panel's data, shared by both copies of the dashboard.

A SEPARATE MODULE FROM mars_census.py, deliberately. app.py must NOT import
mars_census: that would drag requests, pdfplumber and update_index into the
Streamlit process, which is the precise reason bucketing.py exists as its own
file. This reads two small tables and imports nothing but pandas and the DB
shim. The census needs the network, so it cannot run on the page at all -- the
pipeline does the network work once and leaves its answer in a table.

THREE STATES, and the third is why this is a module rather than three lines in
each app.py:

    clean        the census ran and found nothing.
    findings     the census ran and found something.
    unavailable  the census did not run, or its tables cannot be read.

"unavailable" MUST NEVER RENDER AS "clean". A run that found nothing and a run
that never happened are both zero findings; only the runs row tells them
apart. Collapsing the two is the exact bug the two-table split in
mars_census.py exists to prevent, and it would be undone here, in the one
place a reader actually looks.

CLIENTS READ THIS PAGE, so the wording is "AMS reconciliation ... no
discrepancies" -- QA, not alarm. A future edit that makes it louder, or that
puts a raw phantom row and its price on the public page, turns an internal
quality line into a client question about data quality. The count line is
visible without clicking, because a reassurance behind an expander is not a
reassurance; the per-row detail is not.
"""
import pandas as pd

import snowflake_db as db

CLEAN = "clean"
FINDINGS = "findings"
UNAVAILABLE = "unavailable"

# Mirrors app.py's _STALE_WARN_HOURS. A census frozen at yesterday's run must
# read as frozen rather than as this morning's reassurance -- the panel is only
# worth anything if the reader can tell which morning it describes.
STALE_HOURS = 20

_RUN_COLS = ("run_at", "window_start", "window_end", "n_compared",
             "n_phantom", "n_missing", "n_withheld")


def census_state(conn):
    """
    (state, summary, DataFrame) for the most recent census run.

    summary is None whenever state is "unavailable", so a caller cannot read a
    count off a run that did not happen.

    BOTH READS MUST SUCCEED. The findings table failing to open while the runs
    row is readable is a different failure from a clean morning, and returning
    "clean" with an empty frame would report the second while looking at the
    first.
    """
    try:
        runs = db.read_sql_lower(
            "SELECT run_at, window_start, window_end, n_compared, n_phantom, "
            "n_missing, n_withheld FROM mars_census_runs", conn)
        rows = db.read_sql_lower(
            "SELECT kind, slug_id, location, raw_date, report_date, "
            "index_date, weight_low, muscle_grade, head_count, avg_weight, "
            "avg_price, detail FROM mars_census", conn)
    except Exception:                       # noqa: BLE001 -- tables absent
        return UNAVAILABLE, None, pd.DataFrame()
    if runs.empty:
        return UNAVAILABLE, None, pd.DataFrame()

    run = runs.sort_values("run_at").iloc[-1]
    summary = {c: run[c] for c in _RUN_COLS}
    found = sum(int(summary[c] or 0)
                for c in ("n_phantom", "n_missing", "n_withheld"))
    return (FINDINGS if found else CLEAN), summary, rows


def _stamp(run_at):
    """run_at as "Sep 28 at 7:41 AM", or whatever it is if it will not parse."""
    try:
        t = pd.to_datetime(run_at).to_pydatetime()
    except Exception:                       # noqa: BLE001
        return str(run_at)
    # " 0" -> " " strips the leading zero from the day and the hour both;
    # %-I is not portable to Windows, where this job actually runs.
    return t.strftime("%b %d at %I:%M %p").replace(" 0", " ")


def _age_hours(run_at, now):
    if now is None:
        return None
    try:
        t = pd.to_datetime(run_at).to_pydatetime()
    except Exception:                       # noqa: BLE001
        return None
    return (now - t.replace(tzinfo=None)).total_seconds() / 3600.0


def headline(state, summary, now=None):
    """
    The one line that is always on the page.

    IT IS ALWAYS ON THE PAGE. "no discrepancies" every ordinary morning is what
    makes one morning's finding mean anything, and a line that appears only
    when something is wrong is a line nobody learns to read -- the same
    argument the barn report is built on.

    It names the WINDOW IT JUDGED rather than implying full coverage. The
    scheduled census sees roughly the last eight sale days; anything that goes
    stale older than that is not in this sentence and never will be.
    """
    if state == UNAVAILABLE or not summary:
        # NOT "no discrepancies". The check did not report.
        return ("AMS reconciliation unavailable — whether our stored auction "
                "rows still match USDA could not be determined.")

    stale = ""
    hours = _age_hours(summary["run_at"], now)
    if hours is not None and hours >= STALE_HOURS:
        stale = f" This check last ran {hours:.0f} hours ago."

    head = (f"AMS reconciliation {_stamp(summary['run_at'])} — "
            f"{int(summary['n_compared'])} sale day(s) over "
            f"{summary['window_start']} to {summary['window_end']} checked "
            f"against USDA")
    if state == CLEAN:
        return f"{head}, no discrepancies.{stale}"
    return (f"{head}: {int(summary['n_phantom'])} row(s) we hold that USDA no "
            f"longer publishes, {int(summary['n_missing'])} published row(s) "
            f"we do not hold, {int(summary['n_withheld'])} report(s) that "
            f"could not be checked.{stale}")
