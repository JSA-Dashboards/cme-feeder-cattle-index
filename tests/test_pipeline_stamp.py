"""
The freshness stamp must advance on EVERY run, not once per slot.

2026-10-07: a mid-day correction took index date 10-06 from 337.9470 to
337.8693 and pushed it, and the dashboard went on saying "Last refreshed Oct 7
at 8:05 AM Central (1.6h ago)" while the data on screen was five minutes old.

The cause was not a bug in snapshots. app.py read MAX(captured_at) FROM
fci_snapshots, and fci_snapshots is INSERT-OR-IGNORE on
(index_date, run_date, run_slot) -- the FIRST run of a slot wins and nothing
later moves it. That immutability is the entire point of snapshots: it is what
keeps the head-to-head against CME honest, because a later re-run cannot
retroactively improve what we said in the morning. The column was answering
"what did we say?" and the stamp was asking "when did data last land?". Those
agree every ordinary day and diverge the moment a run repeats inside a slot,
which is exactly what a mid-day correction is.

So the tests below are in two halves. The ones about pipeline_stamp are about
the stamp MOVING. The ones about fci_snapshots are about it NOT moving -- if a
future change makes snapshots mutable to "fix" freshness, the morning call
stops being a record of the morning and this file should go red.
"""
import sqlite3
from datetime import datetime, timedelta

import pytest

import update_index as ui
import snapshots


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.delenv("USE_SNOWFLAKE", raising=False)
    c = sqlite3.connect(":memory:")
    c.executescript("""
        CREATE TABLE pipeline_stamp (
            id INTEGER PRIMARY KEY, written_at TEXT NOT NULL,
            run_date TEXT NOT NULL, run_slot TEXT NOT NULL);
        CREATE TABLE fci_daily (
            report_date TEXT PRIMARY KEY, fci_value REAL NOT NULL,
            n_locations INTEGER, total_head INTEGER, same_day_price REAL,
            same_day_head INTEGER, same_day_avg_weight REAL);
        CREATE TABLE fci_snapshots (
            index_date TEXT NOT NULL, run_date TEXT NOT NULL,
            run_slot TEXT NOT NULL, captured_at TEXT NOT NULL,
            fci_value REAL NOT NULL, total_head INTEGER, n_locations INTEGER,
            PRIMARY KEY (index_date, run_date, run_slot));
    """)
    return c


def stamp(conn):
    row = conn.cursor().execute(
        "SELECT MAX(written_at) FROM pipeline_stamp").fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# The stamp moves.
# ---------------------------------------------------------------------------

def test_a_run_records_a_stamp(conn):
    t = datetime(2026, 10, 7, 7, 56, 28)
    ui.stamp_pipeline_run(conn, now=t)
    assert stamp(conn) == "2026-10-07T07:56:28"


def test_a_SECOND_run_in_the_SAME_SLOT_advances_it(conn):
    """
    THE 2026-10-07 FAILURE. Both runs are in the 'am' slot on the same day --
    the shape of every mid-day correction -- and the stamp must still move.
    A merge_ignore here instead of merge_replace reproduces the bug exactly.
    """
    ui.stamp_pipeline_run(conn, now=datetime(2026, 10, 7, 7, 56, 28))
    ui.stamp_pipeline_run(conn, now=datetime(2026, 10, 7, 9, 30, 22))
    assert stamp(conn) == "2026-10-07T09:30:22"


def test_it_stays_one_row_however_often_it_runs(conn):
    """A stamp that appended would grow a row per run forever and make
    MAX() scan an ever-larger table for a single value."""
    for hour in (7, 9, 13, 15):
        ui.stamp_pipeline_run(conn, now=datetime(2026, 10, 7, hour, 0, 0))
    n = conn.cursor().execute("SELECT COUNT(*) FROM pipeline_stamp").fetchone()[0]
    assert n == 1


def test_the_stamp_is_COMMITTED_not_just_written(conn):
    """
    THE ONE THE FIRST VERSION OF THIS FILE MISSED, and it shipped because of it.

    db.merge_replace does not commit, and capture_snapshots commits once after
    its whole loop -- so stamp_pipeline_run wrote the row, returned, and lost it
    when the connection closed. The 09:50 run on 2026-10-07 reported success and
    left pipeline_stamp EMPTY.

    Every other test here passed throughout, because sqlite3 shows uncommitted
    writes back on the same connection. Rolling back is what makes the
    difference visible: a committed row survives it, an uncommitted one does
    not.
    """
    ui.stamp_pipeline_run(conn, now=datetime(2026, 10, 7, 9, 50, 12))
    conn.rollback()
    assert stamp(conn) == "2026-10-07T09:50:12", (
        "the stamp did not survive a rollback, so it was never committed and "
        "will be lost when the pipeline's connection closes")


def test_it_records_the_slot_it_ran_in(conn):
    ui.stamp_pipeline_run(conn, now=datetime(2026, 10, 7, 7, 56, 0))
    assert conn.cursor().execute(
        "SELECT run_slot FROM pipeline_stamp").fetchone()[0] == "am"
    ui.stamp_pipeline_run(conn, now=datetime(2026, 10, 7, 13, 2, 0))
    assert conn.cursor().execute(
        "SELECT run_slot FROM pipeline_stamp").fetchone()[0] == "pm"


def test_the_stamp_is_naive_local_not_utc(conn):
    """
    app.py compares this against Central. Streamlit Cloud runs in UTC, so a
    tz-aware or UTC stamp would read every run as five hours fresher than it
    is -- the one direction the freshness check must never fail in.
    """
    t = datetime(2026, 10, 7, 9, 30, 22)
    written = ui.stamp_pipeline_run(conn, now=t)
    assert written.tzinfo is None
    assert datetime.fromisoformat(stamp(conn)) == t


# ---------------------------------------------------------------------------
# Snapshots do NOT move, and must not be "fixed" to.
# ---------------------------------------------------------------------------

def _one_estimate(conn, value=337.9470):
    conn.execute("DELETE FROM fci_daily")
    conn.execute("INSERT INTO fci_daily VALUES ('2026-10-06',?,270,1892,343.0,1892,812.0)",
                 (value,))
    conn.commit()


def test_a_repeat_run_in_a_slot_does_NOT_move_the_snapshot(conn):
    """
    The behaviour the stamp was wrongly reading. This is CORRECT and load
    bearing: the morning call is what we told clients at the time, and a
    mid-day re-run must not rewrite it. If this ever goes red, the scorecard
    against CME has started grading a number nobody published.
    """
    morning = datetime(2026, 10, 7, 7, 56, 28)
    _one_estimate(conn, 337.9470)
    snapshots.capture_snapshots(conn, now=morning)
    first = conn.cursor().execute(
        "SELECT captured_at, fci_value FROM fci_snapshots").fetchall()

    _one_estimate(conn, 337.8693)          # the correction
    n = snapshots.capture_snapshots(conn, now=datetime(2026, 10, 7, 9, 30, 22))
    after = conn.cursor().execute(
        "SELECT captured_at, fci_value FROM fci_snapshots").fetchall()

    assert n == 0, "a repeat run in the same slot must freeze nothing"
    assert after == first, "the morning call must survive the correction"


def test_the_two_disagree_after_a_correction_and_that_is_the_point(conn):
    """
    The whole reason for a separate table, asserted directly: after a same-slot
    re-run the snapshot is stale and the stamp is current. Reading freshness off
    the snapshot is what produced "1.6h ago" about five-minute-old data.
    """
    _one_estimate(conn, 337.9470)
    snapshots.capture_snapshots(conn, now=datetime(2026, 10, 7, 7, 56, 28))
    ui.stamp_pipeline_run(conn, now=datetime(2026, 10, 7, 7, 56, 28))

    _one_estimate(conn, 337.8693)
    snapshots.capture_snapshots(conn, now=datetime(2026, 10, 7, 9, 30, 22))
    ui.stamp_pipeline_run(conn, now=datetime(2026, 10, 7, 9, 30, 22))

    snap = conn.cursor().execute(
        "SELECT MAX(captured_at) FROM fci_snapshots").fetchone()[0]
    assert snap == "2026-10-07T07:56:28", "snapshot pinned to the first run"
    assert stamp(conn) == "2026-10-07T09:30:22", "stamp follows the last run"
    assert datetime.fromisoformat(stamp(conn)) > datetime.fromisoformat(snap)


# ---------------------------------------------------------------------------
# Where it sits in the push.
# ---------------------------------------------------------------------------

def test_the_stamp_is_pushed_LAST_among_the_critical_tables():
    """
    Its POSITION is the guarantee. A current stamp is only evidence that the
    data landed if everything else went first; hoist it up the list and a push
    that dies halfway leaves a fresh stamp sitting on stale numbers, which is
    precisely the failure _load_last_refresh() exists to detect.
    """
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "migrate", Path(__file__).resolve().parent.parent
        / "snowflake" / "02_migrate_data.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert "pipeline_stamp" in mod.CRITICAL_TABLES
    assert mod.CRITICAL_TABLES[-1] == "pipeline_stamp", mod.CRITICAL_TABLES
    assert "pipeline_stamp" not in mod.OPTIONAL_TABLES, (
        "OPTIONAL is pushed after the index and a failure there is not reported "
        "as an index failure -- the freshness stamp cannot live there")


def test_the_table_is_created_by_the_pipeline_not_by_hand():
    src = (ui.__file__ and open(ui.__file__, encoding="utf-8").read())
    assert "CREATE TABLE IF NOT EXISTS pipeline_stamp" in src
