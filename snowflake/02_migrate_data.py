"""
One-time (re-runnable) migration: bulk-loads every row from the current
SQLite database (data/mars_history.db) into the JSA.CME_FEEDER_CATTLE
Snowflake schema. Uses write_pandas with TRUNCATE-then-load per table, so
it's safe to re-run (idempotent) if SQLite picks up newer data before the
Snowflake cutover is fully confirmed.

    python snowflake/02_migrate_data.py

EXIT CODES
    0   everything pushed; contents verified (optional-table failures still
        exit 0 -- see OPTIONAL_TABLES below)
    1   a CRITICAL table failed to load, or the push ran out of its time
        budget before reaching one. Its transaction ROLLED BACK (or never
        began), so Snowflake still holds its previous contents and the
        dashboard is STALE but self-consistent.
    3   every table loaded, but a critical table's contents were not CONFIRMED
        to match local SQLite -- either they demonstrably differ, or nothing
        about them was verified at all (0 rows, or no column shared between
        the two schemas). The write COMMITTED: Snowflake holds NEW data that
        may be wrong or may be empty, so the dashboard may be serving WRONG
        values. That is a different and worse situation than 1, which is why
        it has its own code -- daily_update.ps1 branches on it and says so.
"""
import os
import sqlite3
import sys
import time
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

HERE = Path(__file__).parent.parent
DB_PATH = HERE / "data" / "mars_history.db"

# CRITICAL is the feeder cattle index itself: if any of these fails to land,
# the dashboard is serving stale numbers and somebody needs to know tonight.
CRITICAL_TABLES = ["fci_daily", "mars_sales", "cme_ftp_daily",
                   "cme_ftp_locations", "cme_ftp_brackets", "peer_estimates",
                   "fci_snapshots"]

# OPTIONAL feeds the supply-side dashboards. A failure here means one tab is
# stale; it must NOT be reported as an index failure. Before this split, any
# border-table problem aborted the whole push with a non-zero exit, which
# daily_update.ps1 read as push_exit != 0 -- logging "DASHBOARD IS STALE" and
# emailing a failure about an index that was already safely committed, since
# the critical tables are pushed first and each table commits on its own.
#
# mars_census/_runs come FIRST, and are OPTIONAL rather than CRITICAL on
# purpose: a census push failure must never be reported as an index failure.
# Both are a handful of rows, so putting them ahead of the 73k-row tables
# costs nothing and means an unrelated failure later in the list does not
# freeze the reconciliation panel at yesterday's answer.
#
# BOTH WERE CONFIRMED TO EXIST IN SNOWFLAKE BEFORE BEING LISTED HERE. main()
# runs DESC TABLE outside its try, so a name listed here that does not exist
# there raises straight out of the loop and fails the ENTIRE optional push --
# every dashboard table after it goes stale, for a diagnostic.
OPTIONAL_TABLES = ["mars_census", "mars_census_runs",
                   "replacement_sales", "feeder_receipts", "border_reports",
                   "census_cattle_imports", "border_receipts",
                   "border_volumes", "border_prices",
                   "calf_sales", "corn_bids", "distillers_bids", "hay_bids"]

TABLES = CRITICAL_TABLES + OPTIONAL_TABLES


# --- PUTTING A CLOCK ON THE PUSH --------------------------------------------
#
# NOTHING BOUNDED ANY OF THIS, and the failure it allows has already happened.
# The 13:00 run on 2026-09-10 hung and was killed by hand more than two hours
# later: logs/update_2026-09-10.log shows "run started 2026-09-10 13:00:02",
# one healthcheck line, and then nothing until the 15:09 rerun.
# daily_update.ps1 runs every step under `Start-Process -Wait` with no
# timeout, so a statement that never returns strands a FINISHED index
# unpublished, and the only signal is the dead-man's switch noticing the
# absence of a ping. That incident is why the dead-man's switch exists.
#
# MEASURED, NOT CHOSEN BY TASTE. Read-only against the live account,
# 2026-09-30:
#
#     STATEMENT_TIMEOUT_IN_SECONDS   172800  (48 hours) on BOTH the session
#                                    and SNOWFLAKE_LEARNING_WH -- i.e. the
#                                    Snowflake default, i.e. nothing
#     connector network_timeout      None -- SnowflakeRestful's retry loop
#                                    takes its deadline from this, so it
#                                    retries FOREVER
#     connector socket_timeout       None -> DEFAULT_SOCKET_CONNECT_TIMEOUT,
#                                    60s per HTTP request (already bounded,
#                                    which is why it is left alone: lowering
#                                    it risks breaking write_pandas' upload)
#
# and the push's own statements, worst case over the 7,619 that
# INFORMATION_SCHEMA.QUERY_HISTORY still holds (2026-09-16 .. 2026-09-30):
#
#     DELETE FROM <table>       6.319s   (border_reports, 2026-09-27)
#     COMMIT                    4.831s
#     SELECT COUNT(*)           2.040s   (feeder_receipts)
#     DESC TABLE                1.857s
#     the fingerprint SELECT    1.031s   (worst of 528 recorded)
#
# plus the fingerprint timed live end to end the same day, read-only: 3.01s
# server-side for all 20 tables, worst single table 0.212s.
#
# 120s is 19x the slowest statement this push has ever issued and 116x the
# slowest fingerprint.
#
# THE ONE STATEMENT THAT COULD NOT BE MEASURED THIS WAY, said out loud rather
# than glossed over: write_pandas' PUT and COPY INTO do not appear in
# INFORMATION_SCHEMA.QUERY_HISTORY on this account at all -- zero rows match
# 'COPY INTO %' or 'PUT %' across all 7,619 -- so the heaviest thing the push
# does, loading 72,641 rows into cme_ftp_brackets, has no direct timing here.
# It is bounded indirectly instead, and tightly enough: scripts/cme_pull.ps1
# starts at 10:15:00, fetches CME's files AND pushes three tables including
# cme_ftp_brackets, and logged "finished" at 10:15:32 .. 10:15:39 on every
# retained day. Nothing inside a 35-second step takes 120 seconds.
#
# AND IF THAT IS EVER WRONG, THE FAILURE IS THE SAFE ONE. A cancelled DELETE
# or COPY raises inside the per-table try, which ROLLS BACK: the table keeps
# its previous contents, the push exits 1, and daily_update.ps1 says the
# dashboard is stale -- which would be true. A cancelled FINGERPRINT is
# caught by verify_table and prints INCONCLUSIVE. Neither is a hang, which is
# the only outcome this cannot recover from.
#
# THIS DOES NOT BELONG IN snowflake_db.py. That module is shared byte-for-byte
# with the deployed dashboard across six copies, and the dashboard runs
# legitimately slow queries -- the query-history reads used to gather the
# numbers above took 19.7s, 27.6s and 54.8s through that same get_conn(), and
# a 120s cap would have come uncomfortably close to killing the last of them.
# A bound that is right for a push is wrong for a dashboard. Setting it here,
# on the push's own session, also means it covers the PRE-EXISTING DELETE,
# write_pandas and SELECT COUNT(*) and not merely the new check.
STATEMENT_TIMEOUT_SECONDS = 120

# The client-side half, and it is a different failure. STATEMENT_TIMEOUT is
# enforced by the SERVER: it cancels the query, but if the response never
# arrives the client still waits. Each HTTP request is already capped at 60s
# by the connector's default socket timeout; what is unbounded is the RETRY
# LOOP around it. Set ABOVE the statement timeout on purpose, so when a
# statement runs long the server's own cancellation wins the race and the push
# gets the explanatory error ("Statement reached its statement or warehouse
# timeout") rather than a bare client-side give-up.
NETWORK_TIMEOUT_SECONDS = 300

# And a bound on the WHOLE push, checked between tables. The two above bound
# one statement each; 20 tables x ~9 statements x 120s is still six hours,
# which is the 2026-09-10 shape all over again. A full 20-table push costs
# well under a minute today, so 15 minutes is more than an order of magnitude
# of headroom and still finishes long before the next slot.
PUSH_BUDGET_SECONDS = 900


def bound_session(sf_conn):
    """
    Put a clock on this connection, both ends, and SAY what is in force.

    Returns the list of note strings, which is also what gets printed. Never
    raises: a push that cannot set a timeout is worse off than one that can,
    but it is not worse off than not running. What it must never do is stay
    quiet about it -- an unbounded push that says it is bounded is how the
    2026-09-10 hang gets to happen twice.
    """
    notes = []
    try:
        sf_conn.cursor().execute(
            f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = "
            f"{STATEMENT_TIMEOUT_SECONDS}")
        notes.append(f"statements {STATEMENT_TIMEOUT_SECONDS}s")
    except Exception as e:                      # noqa: BLE001
        notes.append(f"STATEMENTS UNBOUNDED ({type(e).__name__})")

    # The connector exposes network_timeout as a read-only property over
    # _network_timeout, and network.py reads it fresh on every request -- so
    # assigning the attribute after connect() is the only way to set it
    # without editing the shared snowflake_db.py. Then READ IT BACK through
    # the public property: a connector upgrade that renames the attribute must
    # show up as "RETRIES UNBOUNDED" in the log, not as a silent no-op leaving
    # exactly the infinite retry loop this exists to close.
    try:
        sf_conn._network_timeout = NETWORK_TIMEOUT_SECONDS
    except Exception:                           # noqa: BLE001
        pass
    if getattr(sf_conn, "network_timeout", None) == NETWORK_TIMEOUT_SECONDS:
        notes.append(f"retries {NETWORK_TIMEOUT_SECONDS}s")
    else:
        notes.append("RETRIES UNBOUNDED")

    notes.append(f"whole push {PUSH_BUDGET_SECONDS}s")
    line = f"session bounds: {', '.join(notes)}"
    # check_run.ps1's morning digest filters the log on 'WARN', so an
    # unbounded push has to carry that word to be seen at all.
    print(line if "UNBOUNDED" not in line else f"WARNING: {line}")
    return notes


def unreadable(sqlite_conn, sf_conn, table):
    """
    Why `table` cannot be pushed at all, or None. Both ends, because both
    matter and both fail the same way.

    WHY THIS IS A PREFLIGHT AND NOT PART OF THE LOOP'S OWN try. In main() the
    local read and the Snowflake DESC both sit OUTSIDE the transaction's try,
    so either one raising takes down the whole remaining push -- every table
    after it in the list goes stale. For a CRITICAL table that is right. For an
    OPTIONAL table it is exactly backwards: the split exists so that one feed's
    problem costs one dashboard tab and is not reported as an index failure.

    It bites for real rather than hypothetically. mars_census and
    mars_census_runs are created locally by update_index.py's census call,
    which is deliberately wrapped in a guard that lets it fail without failing
    the run -- so a run where the census failed leaves those tables absent, and
    with them first in OPTIONAL_TABLES that would strand every dashboard table
    behind them. A diagnostic must not be able to do that either.
    """
    try:
        sqlite_conn.execute(f"SELECT 1 FROM {table} LIMIT 1")
    except Exception as e:                      # noqa: BLE001
        return f"absent from SQLite ({type(e).__name__}: {e})"
    try:
        sf_conn.cursor().execute(f"DESC TABLE {table}")
    except Exception as e:                      # noqa: BLE001
        return f"absent from Snowflake ({type(e).__name__}: {e})"
    return None


def main(only=None, group=None):
    """
    Push SQLite -> Snowflake. `only` restricts the push to a subset of tables,
    which is what the 10:15 CME-print pull uses: it changes two tables and has
    no business spending a minute re-uploading 73k replacement sales and 72k
    bracket rows to land them.

    `group` is "critical" or "optional" and exists so the daily job can publish
    the index BEFORE spending eight minutes ingesting auction and corn data it
    does not need. The split lives here rather than in daily_update.ps1 on
    purpose: a table added to OPTIONAL_TABLES above must not require a matching
    edit to a PowerShell array that nobody would remember to make, and the
    failure mode of forgetting -- a table that is never pushed at all -- is
    silent.
    """
    from snowflake.connector.pandas_tools import write_pandas

    # Auth goes through snowflake_db.get_conn() so there is exactly one
    # credential path in the codebase (key-pair, with a password fallback).
    # USE_SNOWFLAKE is forced on here: this script's whole job is the upload,
    # regardless of which backend the app itself is pointed at.
    sys.path.insert(0, str(HERE))
    os.environ["USE_SNOWFLAKE"] = "1"
    import snowflake_db as db
    import push_verify

    # The whole-push clock starts before the connection, because a login
    # that never returns is the same outage as a statement that never does.
    started = time.monotonic()
    sf_conn = db.get_conn()
    # BEFORE ANY OTHER STATEMENT, and that placement is half the value.
    # bound_session existed and nothing called it, which bounds nothing at
    # all. Calling it here is what puts a clock on the PRE-EXISTING DELETE,
    # write_pandas and SELECT COUNT(*) -- all three are older than the
    # content check and were never covered by anything -- and not merely on
    # the new fingerprint.
    bound_session(sf_conn)
    sqlite_conn = sqlite3.connect(DB_PATH)
    failed_optional = []
    # Tables that LOADED but whose contents do not match local SQLite. Kept
    # apart from failed_optional because the two mean opposite things about
    # what Snowflake now holds: a load failure rolled back, a content failure
    # committed.
    content_failed = []
    # Tables whose contents could not be checked AT ALL -- the fingerprint
    # itself failed. Tracked rather than only printed, because "the check did
    # not run" and "the check passed" must not reach the end of the push
    # looking the same. See the summary block below for why this does NOT
    # change the exit code.
    unverified = []
    # CRITICAL tables where the check RAN, did not error, and still confirmed
    # nothing: 0 rows on both sides, or two schemas sharing no column. A THIRD
    # list and not a flavour of either above, because the three mean three
    # different things -- content_failed DIFFERS from SQLite, unverified could
    # not be looked at, and this one was looked at and had nothing to see.
    # This one DOES change the exit code; unverified does not. Why they differ
    # is argued at the two summary blocks below.
    unconfirmed = []

    tables = TABLES
    if group:
        tables = {"critical": CRITICAL_TABLES, "optional": OPTIONAL_TABLES}[group]
        print(f"pushing the {group} tables: {', '.join(tables)}")
    if only:
        unknown = [t for t in only if t not in TABLES]
        if unknown:
            raise SystemExit(f"unknown table(s): {', '.join(unknown)}")
        tables = [t for t in tables if t in only]   # keep the critical-first order
        print(f"pushing {len(tables)} of {len(TABLES)} tables: {', '.join(tables)}")

    for table in tables:
        # THE WHOLE-PUSH CLOCK, CHECKED BETWEEN TABLES. The two session bounds
        # above cap ONE statement each. Twenty tables x nine statements x 120s
        # is still six hours, which is the 2026-09-10 shape all over again:
        # daily_update.ps1 waits on this under Start-Process -Wait with no
        # timeout of its own, so a slow push leaves a FINISHED index
        # unpublished for as long as it takes.
        #
        # It stops BETWEEN tables and never inside one. Each table's DELETE
        # and reload is a single transaction, and cutting one in half is the
        # only outcome worse than being late. So every table already pushed is
        # committed and correct, and every table not reached was never
        # opened -- nothing was DELETEd for it and Snowflake still holds its
        # previous contents.
        elapsed = time.monotonic() - started
        if elapsed > PUSH_BUDGET_SECONDS:
            remaining = tables[tables.index(table):]
            left_critical = [t for t in remaining if t in CRITICAL_TABLES]
            print("")
            print(f"WARNING: the push has spent {elapsed:.0f}s, past its "
                  f"{PUSH_BUDGET_SECONDS}s budget, with {len(remaining)} "
                  f"table(s) still to go: {', '.join(remaining)}. Stopping "
                  f"here rather than running into the next scheduled slot.")
            if left_critical:
                # Exit 1, the rollback code, and here it is the honest one:
                # these tables were never touched, so daily_update.ps1's
                # "Snowflake still holds its previous contents" is TRUE of
                # them. That is the whole difference between 1 and 3, and it
                # is why an overrun is not simply exit 3.
                print(f"ERROR: {len(left_critical)} CRITICAL table(s) were "
                      f"never pushed: {', '.join(left_critical)}. Nothing was "
                      f"deleted or written for them, so Snowflake still holds "
                      f"their previous contents - the index tables are STALE, "
                      f"not wrong.")
                raise SystemExit(1)
            # Only dashboard tables left. The index published fine and
            # reporting that as a push failure is the same harm the
            # CRITICAL/OPTIONAL split exists to avoid, so these join the
            # skipped list and the run still exits 0.
            failed_optional.extend(t for t in remaining
                                   if t not in failed_optional)
            break

        # Optional tables only: a critical table that cannot be read is a real
        # failure and must still stop the push. See unreadable().
        if table in OPTIONAL_TABLES:
            problem = unreadable(sqlite_conn, sf_conn, table)
            if problem:
                print(f"{table}: SKIPPED - {problem}")
                failed_optional.append(table)
                continue

        df = pd.read_sql(f"SELECT * FROM {table}", sqlite_conn)
        sqlite_count = len(df)
        # Snowflake column names are case-insensitive when unquoted, but
        # write_pandas matches against the table's actual (uppercase) column
        # names -- uppercase the DataFrame's columns so they line up.
        df.columns = [c.upper() for c in df.columns]

        cur = sf_conn.cursor()

        # Only upload columns the target table actually has. update_index.py can
        # add a column to SQLite (init_db migrates it) while the Snowflake table
        # still lacks it, because ALTER there needs MODIFY, which SYSADMIN was
        # not granted. Without this, write_pandas fails on the whole table for
        # one absent column -- and none of the extras are read by app.py, so
        # dropping them costs the dashboard nothing.
        target_cols = {r[0].upper() for r in cur.execute(f"DESC TABLE {table}")}
        extra = [c for c in df.columns if c not in target_cols]
        if extra:
            print(f"{table}: skipping column(s) absent in Snowflake: {', '.join(extra)}")
            df = df[[c for c in df.columns if c in target_cols]]

        # DELETE, not TRUNCATE: Snowflake gates TRUNCATE behind its own
        # privilege, which SYSADMIN was not granted on these tables (it has
        # SELECT/INSERT/UPDATE/DELETE only, and ACCOUNTADMIN owns them).
        # DELETE is also transactional, which TRUNCATE-then-load was not --
        # wrapping the swap means a failed upload can no longer leave the
        # dashboard reading an empty table.
        cur.execute("BEGIN")
        try:
            cur.execute(f"DELETE FROM {table}")
            success, nchunks, nrows, _ = write_pandas(sf_conn, df, table.upper())
            if not success:
                raise RuntimeError(f"write_pandas reported failure for {table}")
            cur.execute("COMMIT")
        except Exception as e:
            cur.execute("ROLLBACK")
            print(f"{table}: FAILED - rolled back, table left as it was")
            if table in CRITICAL_TABLES:
                raise
            # Optional table: report it and keep going. Each table commits
            # independently, so the ones already pushed are safe.
            print(f"{table}: NON-CRITICAL - continuing. {type(e).__name__}: {e}")
            failed_optional.append(table)
            continue
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        sf_count = cur.fetchone()[0]

        status = "OK" if sf_count == sqlite_count else "MISMATCH"

        # THE COUNT ABOVE IS BLIND TO EVERY VALUE IN THE TABLE, which is how
        # the 2026-09-29 incident printed [OK] on all seven critical tables
        # while 388 of fci_daily's 949 rows held wrong index values. The
        # fingerprint below compares per-column aggregates computed
        # server-side on both backends; the count stays as the cheap first
        # signal so the log line keeps its shape and stays greppable.
        #
        # Run AFTER the COMMIT on purpose: the DELETE/INSERT invalidates
        # Snowflake's result cache, so there is no stale-read risk, and a
        # fingerprint taken inside the transaction would be measuring an
        # uncommitted state nobody will ever read.
        #
        # verify_table NEVER RAISES. The index is already committed here and
        # a hiccup in the check must not turn a successful push into a
        # failure; it reports INCONCLUSIVE instead, which prints differently
        # from OK so the guard cannot silently decay into the count check it
        # replaced. Columns the push dropped were already reported above.
        verdict = push_verify.verify_table(sqlite_conn, sf_conn, table)
        print(f"{table}: sqlite={sqlite_count} snowflake={sf_count} "
              f"[{status}] {verdict.summary()}")
        for line in push_verify.describe(verdict.differences):
            print(f"    {line}")
        if verdict.unclassified:
            print(f"    NOTE: {', '.join(verdict.unclassified)} have a type this "
                  f"check cannot fingerprint; their values are NOT verified.")
        # THE TWO SETS OF COLUMNS NOBODY IS COMPARING, SAID OUT LOUD.
        #
        # verdict.dropped is the same set the "skipping column(s)" line above
        # names, derived independently by push_verify from its own DESC TABLE.
        # Printing it from the VERDICT is not duplication -- that line says
        # what the UPLOAD dropped and this one says what the CHECK is blind
        # to -- and it is the only thing that makes the field load-bearing:
        # until now, deleting `dropped` from the Verdict entirely broke no
        # test.
        if verdict.dropped:
            print(f"    NOTE: present only in SQLite, so not uploaded and NOT "
                  f"verified: {', '.join(verdict.dropped)}")
        # And the other direction, which has no SQLite side to compare
        # against at all. pushed_columns() reported only SQLite-side drops
        # while its docstring claimed both sets were "rediscovered here on
        # every run". After DELETE + write_pandas a Snowflake-only column is
        # NULL on every row in production, and the push printed [CONTENT OK]
        # straight over it. It cannot be compared, so it is REPORTED -- and
        # push_verify additionally asserts the one thing that must be true of
        # it, that COUNT(col) is 0, which arrives here as a MISMATCH if it is
        # not.
        if verdict.snowflake_only:
            print(f"    NOTE: present only in Snowflake, so NULL on every row "
                  f"after this push and with no local column to compare "
                  f"against: {', '.join(verdict.snowflake_only)}")

        # A count mismatch counts too, so the cheap signal keeps its teeth on
        # the runs where the fingerprint itself could not complete.
        if verdict.status == "MISMATCH" or status == "MISMATCH":
            content_failed.append(table)
            scope = "CRITICAL" if table in CRITICAL_TABLES else "non-critical"
            print(f"{table}: CONTENT VERIFICATION FAILED ({scope}). The load "
                  f"COMMITTED - Snowflake holds new data that does not match "
                  f"local SQLite.")
        elif verdict.status == "INCONCLUSIVE":
            unverified.append(table)
            # Named the same way the MISMATCH line above names it. Without the
            # scope, fci_daily going unchecked and hay_bids going unchecked
            # print identically, and the one line in twenty that says the
            # INDEX was not verified is indistinguishable from the one that
            # says a dashboard tab was not.
            scope = "CRITICAL" if table in CRITICAL_TABLES else "non-critical"
            print(f"{table}: WARNING - content verification could not run "
                  f"({scope}). The load itself succeeded; the contents are "
                  f"UNVERIFIED, not confirmed.")
        elif not verdict.verified:
            # EMPTY, NO COLUMNS, or any status added after this was written.
            #
            # KEYED ON `verified`, NOT ON A LIST OF STATUS NAMES. `verified`
            # is default-deny -- "a status added later is not-verified by
            # default rather than a silent pass" -- and that promise is only
            # true if the push agrees with it. `verdict.status in ("EMPTY",
            # "NO COLUMNS")` would behave identically today and let the next
            # status through with exit 0.
            #
            # THIS BRANCH DID NOT EXIST, and both this file's exit-code
            # docstring and push_verify's Verdict.verified said it did. An
            # EMPTY fci_daily means the push DELETEd Snowflake's rows and
            # reloaded none: the dashboard serves no index, every aggregate
            # is NULL == NULL, the row count agrees at 0 == 0, and the push
            # printed [CONTENT EMPTY] and exited 0. NO COLUMNS means rows on
            # both sides but nothing shared to compare, so the only check was
            # COUNT(*) -- the exact blind spot of 2026-09-29, under a verdict
            # line saying the table was checked.
            #
            # NOT folded into content_failed: those tables demonstrably
            # DIFFER from local SQLite and these agree with it -- an empty
            # table matches an empty table exactly. Sending somebody to hunt
            # a per-column difference that does not exist burns the first ten
            # minutes of an incident.
            #
            # CRITICAL ONLY, and the reason is that an OPTIONAL table can
            # legitimately be empty. mars_census and mars_census_runs are
            # written by a census step deliberately allowed to fail without
            # failing the day, and on a freshly built database several
            # dashboard feeds hold nothing until their first ingest. (Counted
            # read-only 2026-09-30, no pushed table is empty right now and
            # mars_census holds one row -- so this is a real possibility
            # rather than today's state, and it must not be quoted as one.)
            # Escalating an empty dashboard feed would put a content failure
            # in the morning digest on any such day,
            # which is CLAUDE.md's "calling this a failure would train
            # someone to ignore a real one" arrived at from the other side.
            # An optional table still prints its own [CONTENT EMPTY] /
            # [CONTENT NO COLUMNS] line, which already says nothing was
            # verified.
            if table in CRITICAL_TABLES:
                unconfirmed.append(table)
                print(f"{table}: CONTENT NOT CONFIRMED (CRITICAL). The load "
                      f"COMMITTED and the check ran without error, but it "
                      f"confirmed nothing about the values.")

    sf_conn.close()
    sqlite_conn.close()

    # Split the same way a load failure is split, and for the same reason: a
    # dashboard table whose contents drifted must not be reported as an index
    # failure, and an index table whose contents drifted must not be quiet.
    critical_content = [t for t in content_failed if t in CRITICAL_TABLES]
    optional_content = [t for t in content_failed if t not in CRITICAL_TABLES]

    # The two lists are disjoint: a table that failed to load `continue`d and
    # never reached the content check. Snapshot the load failures BEFORE
    # folding the content failures in, so the "stale" sentence below never
    # names a table that actually loaded -- they join failed_optional for any
    # caller reading it, but they are not the same problem and must not be
    # described as one.
    skipped_optional = list(failed_optional)
    failed_optional.extend(t for t in optional_content if t not in failed_optional)

    if skipped_optional:
        # Visible in the log, but exit 0: the index published fine, and calling
        # this a failure would train someone to ignore a real one.
        print("")
        print(f"WARNING: {len(skipped_optional)} non-critical table(s) failed "
              f"and were skipped: {', '.join(skipped_optional)}. The feeder "
              f"cattle index published normally; the affected dashboard tab(s) "
              f"will be stale.")
    if optional_content:
        # Deliberately worded apart from the line above. A skipped table is
        # STALE; a content-failed table LOADED and is WRONG, and telling
        # someone to expect yesterday's numbers when the tab is showing
        # something else entirely is how a real incident gets misread.
        print("")
        print(f"WARNING: {len(optional_content)} non-critical table(s) loaded "
              f"but do NOT match local SQLite: {', '.join(optional_content)}. "
              f"The feeder cattle index published normally; the affected "
              f"dashboard tab(s) may be showing wrong values, not stale ones. "
              f"Per-column differences are above.")

    # Already CRITICAL-only by construction (see the branch that fills it),
    # but filtered anyway so the exit-3 decision reads the same way as the
    # content one and cannot drift if that branch is ever widened.
    critical_unconfirmed = [t for t in unconfirmed if t in CRITICAL_TABLES]

    critical_unverified = [t for t in unverified if t in CRITICAL_TABLES]
    if critical_unverified:
        # EXIT CODE DELIBERATELY UNCHANGED -- this prints and returns 0.
        #
        # A check that could not run is not a check that failed. By the time
        # the fingerprint runs the index is already COMMITTED and correct;
        # turning a Snowflake hiccup in the VERIFICATION into a non-zero exit
        # would have daily_update.ps1 log "the DASHBOARD IS STALE" and mail a
        # failure about a push that went perfectly. That is the same harm the
        # CRITICAL/OPTIONAL split was built to avoid, and the same lesson as
        # the skipped-optional block above: "calling this a failure would
        # train someone to ignore a real one."
        #
        # But it must not be invisible either. Left as a single line in the
        # middle of a twenty-table log, a check that is INCONCLUSIVE on every
        # table on every run exits 0 forever while verifying nothing -- which
        # is exactly the silent decay back into the COUNT check that
        # push_verify.py exists to prevent. So it gets a WARNING block beside
        # the other two, where check_run.ps1's digest (which filters the log
        # on 'WARN') will surface it the next morning.
        #
        # What is NOT done here is per-run state. "Unverified every day for a
        # week" is the case that deserves escalation and the push has no
        # memory to detect it with; a block that keeps reappearing in the
        # digest is the honest substitute for one.
        print("")
        print(f"WARNING: {len(critical_unverified)} CRITICAL table(s) loaded "
              f"but their contents could NOT be verified: "
              f"{', '.join(critical_unverified)}. The load itself succeeded "
              f"and the row counts matched; the per-column check did not run, "
              f"so these tables are UNVERIFIED, not confirmed. If this "
              f"repeats, the content check is effectively off and the push is "
              f"back to the row count that missed the 2026-09-29 incident.")

    if critical_content:
        # Exit 3, NOT the generic failure code. daily_update.ps1's message for
        # a non-zero push says "each table rolls back individually, so
        # Snowflake still holds its previous contents" -- true for a load
        # failure and FALSE here, where the write committed and Snowflake
        # holds new but wrong data. Printing that sentence at the exact moment
        # somebody is trying to understand a real incident is worth a
        # dedicated code.
        print("")
        print(f"ERROR: {len(critical_content)} CRITICAL table(s) loaded but "
              f"their contents do NOT match local SQLite: "
              f"{', '.join(critical_content)}. The write COMMITTED - Snowflake "
              f"is serving new data that disagrees with the local index. This "
              f"is the 2026-09-29 shape. Read the per-column differences above "
              f"before re-pushing.")

    if critical_unconfirmed:
        # THE SAME EXIT CODE AS A DEMONSTRATED MISMATCH, AND DELIBERATELY SO:
        # exit 3's meaning is "the write COMMITTED and nobody can say that
        # what Snowflake now serves is right", which covers this exactly. It
        # is NOT exit 1 -- that code promises "Snowflake still holds its
        # previous contents", and it does not; the old rows were DELETEd.
        #
        # Worded apart from the block above because these tables do not
        # disagree with local SQLite. There was nothing to compare, which for
        # an index table most often means the dashboard is serving nothing.
        print("")
        print(f"ERROR: {len(critical_unconfirmed)} CRITICAL table(s) loaded "
              f"but nothing about their contents was confirmed: "
              f"{', '.join(critical_unconfirmed)}. The write COMMITTED. "
              f"Either the table is now EMPTY on both sides, or the two "
              f"schemas share no column and only the row count was compared "
              f"- the check that missed 2026-09-29. An empty index table "
              f"means the dashboard is serving nothing at all, which is "
              f"worse than stale.")

    if critical_content or critical_unconfirmed:
        raise SystemExit(3)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--tables", default=None,
                    help="comma-separated subset to push (default: all)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--critical-only", action="store_true",
                   help="push only the index tables, so it publishes first")
    g.add_argument("--optional-only", action="store_true",
                   help="push only the dashboard tables")
    a = ap.parse_args()
    main(only=[t.strip() for t in a.tables.split(",")] if a.tables else None,
         group=("critical" if a.critical_only else
                "optional" if a.optional_only else None))
