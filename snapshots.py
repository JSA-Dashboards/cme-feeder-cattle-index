"""
Freeze what we said, at the moment we said it.

fci_daily holds ONE row per index date (report_date is its PRIMARY KEY) and is
rewritten from scratch on every run, so our estimate for a given date silently
improves as late-publishing auctions land. Competitors' figures in
peer_estimates are frozen at whatever they printed that morning. Comparing the
two therefore flatters us: our number has seen data theirs never did, and the
scorecard's mean-absolute-miss is measured on settled inputs rather than on the
call we actually made.

The size of that unfairness, measured 2026-09-09 across 80 auctions and 246
reports (sales 07/28-09/09, head-weighted):

  - 83.3% of a sale day's qualifying head is published the SAME day, mostly
    between noon and 19:00. By the next morning at 07:30 we can see 85.2%; by
    noon that day, 95.8%.
  - Almost all of that 10-point gap is one auction. OKC West (El Reno, OK)
    publishes its previous-day sale at a median of +1 day 11:13 and missed a
    07:30 run 7 times out of 7 -- while being the second-largest contributor
    of qualifying head in the sample.
  - Folding El Reno's 09/08 sale (841 head, 681,544 lb, pound-weighted
    $331.64, +$4.21 above the window average) into our 09/08 estimate moves it
    from $327.4306 to $327.7606: +0.33, roughly 80x the 0.004 MAE the
    scorecard reports.

So a fair head-to-head needs our own opening call preserved, not just our
latest one. That is all this table is for; nothing here changes the index
methodology or what fci_daily contains.

Written INSERT-OR-IGNORE on (index_date, run_date, run_slot), so the FIRST
estimate a given slot produced is the one kept. Re-running the morning job
cannot quietly overwrite the morning call it exists to preserve.

SEEDING CAVEAT. The table starts at 2026-09-09, seeded from the fci_daily
values the 07:51 run left behind (recoverable, because that run was the last
thing to touch mars_sales and the recompute is deterministic from it). Those
rows all carry run_date 2026-09-09, so only index date 2026-09-08 has a true
one-day lag; 09-07 and earlier were frozen two or more days after their sale
and had therefore already absorbed some late data. Treat the head-to-head as
fully apples-to-apples only from index date 2026-09-08 onward. Earlier runs
cannot be reconstructed -- their inputs are gone.
"""
from datetime import date, datetime, timedelta

import snowflake_db as db

# Runs at or after this hour are the "settled" afternoon pass; earlier ones are
# the morning call. The pipeline is scheduled at 07:30 and 13:00 local, so any
# boundary between 09:00 and 12:00 separates them; 11 leaves room for a morning
# run that started late (StartWhenAvailable can defer it) without it being
# misfiled as the afternoon pass.
AM_PM_BOUNDARY_HOUR = 11

# How far back to snapshot on each run. Needs to comfortably exceed the gap
# between a sale date and CME printing it (typically 1 publication day, but
# holidays stretch it), so that every date still awaiting a CME print has an
# opening call recorded. Cheap: ~20 rows per run, twice a day.
SNAPSHOT_LOOKBACK_DAYS = 21

# An opening call only counts as one if it was made PROMPTLY after the sale day.
# Because each run snapshots a 21-day lookback, the oldest dates in any given
# run also get a row -- but a value frozen six days after the sale has seen all
# the late-publishing auctions and is a settled number, not a call. Without this
# guard those rows masquerade as opening calls and reintroduce exactly the
# unfairness this module exists to remove.
#
# Normally the lag is 1 day (the next morning's run). 4 allows for the machine
# being off over a weekend without silently dropping a genuine first call.
MAX_OPENING_LAG_DAYS = 4


def run_slot(now: datetime | None = None) -> str:
    """'am' for the morning call, 'pm' for the settled afternoon pass."""
    now = now or datetime.now()
    return "am" if now.hour < AM_PM_BOUNDARY_HOUR else "pm"


def capture_snapshots(conn, lookback_days: int = SNAPSHOT_LOOKBACK_DAYS,
                      now: datetime | None = None) -> int:
    """
    Freeze the current fci_daily estimates for recent index dates.

    Returns the number of rows newly frozen. Zero is the normal, healthy
    result for a repeat run in the same slot -- it means nothing overwrote an
    existing call.
    """
    now = now or datetime.now()
    slot = run_slot(now)
    run_date = now.date().isoformat()
    since = (now.date() - timedelta(days=lookback_days)).isoformat()

    rows = conn.cursor().execute(
        f"SELECT report_date, fci_value, total_head, n_locations FROM fci_daily "
        f"WHERE report_date >= {db.placeholders(1)} ORDER BY report_date",
        (since,),
    ).fetchall()

    # Which dates this slot already froze today. Read once rather than probing
    # per row: the INSERT is idempotent either way, but this is what makes the
    # returned count meaningful without two extra queries per date.
    already = {
        db.iso(r[0])
        for r in conn.cursor().execute(
            f"SELECT index_date FROM fci_snapshots WHERE run_date = {db.placeholders(1)} "
            f"AND run_slot = {db.placeholders(1)}",
            (run_date, slot),
        ).fetchall()
    }

    n = 0
    for report_date, fci_value, total_head, n_locations in rows:
        index_date = db.iso(report_date)
        if index_date in already:
            continue
        db.merge_ignore(
            conn, "fci_snapshots",
            ["index_date", "run_date", "run_slot", "captured_at",
             "fci_value", "total_head", "n_locations"],
            (index_date, run_date, slot, now.isoformat(timespec="seconds"),
             float(fci_value), int(total_head) if total_head is not None else None,
             int(n_locations) if n_locations is not None else None),
            ["index_date", "run_date", "run_slot"],
        )
        n += 1
    conn.commit()
    return n


def opening_calls(conn):
    """
    {index_date_iso: fci_value} -- our FIRST morning estimate for each index
    date made AFTER that sale day had ended.

    Two conditions make this comparable to a competitor's published figure, and
    both matter:

      run_date > index_date -- an 'am' run on the index date itself also
      snapshots that date, but its 7-day window has barely any of that day's
      sales in it yet, so it is a work-in-progress, not a call anyone published.

      run_date - index_date <= MAX_OPENING_LAG_DAYS -- every run snapshots a
      21-day lookback, so old dates pick up rows too. A value frozen a week
      after the sale has already absorbed the late-publishing auctions and is a
      settled number. Counting it as an opening call would put our hindsight up
      against a competitor's same-morning guess.

    A date with no qualifying snapshot is simply absent, which is the honest
    answer for dates that predate this table.
    """
    rows = conn.cursor().execute(
        "SELECT index_date, fci_value, run_date FROM fci_snapshots "
        "WHERE run_slot = 'am' ORDER BY index_date, run_date, captured_at"
    ).fetchall()
    out = {}
    for index_date, fci_value, run_date in rows:
        idx = db.iso(index_date)
        run = db.iso(run_date)
        lag = (date.fromisoformat(str(run)) - date.fromisoformat(str(idx))).days
        if not 0 < lag <= MAX_OPENING_LAG_DAYS:
            continue
        out.setdefault(idx, float(fci_value))   # earliest run_date wins (ordered)
    return out


if __name__ == "__main__":
    # Dependency-free self-check: `python snapshots.py`
    assert run_slot(datetime(2026, 9, 10, 7, 30)) == "am"
    assert run_slot(datetime(2026, 9, 10, 7, 51)) == "am"   # a deferred morning run
    assert run_slot(datetime(2026, 9, 10, 10, 59)) == "am"
    assert run_slot(datetime(2026, 9, 10, 11, 0)) == "pm"
    assert run_slot(datetime(2026, 9, 10, 13, 0)) == "pm"
    # The lag guard. Seeding 21 dates at one timestamp initially made
    # opening_calls() report a settled 6-day-old value as an opening call --
    # reintroducing the very unfairness this module removes. Guard it directly.
    class _FakeCur:
        def __init__(self, rows): self._rows = rows
        def execute(self, *a, **k): return self
        def fetchall(self): return self._rows
    class _FakeConn:
        def __init__(self, rows): self._rows = rows
        def cursor(self): return _FakeCur(self._rows)

    _rows = [
        ("2026-09-08", 327.4306, "2026-09-09"),   # lag 1  -> a real opening call
        ("2026-09-03", 329.5350, "2026-09-09"),   # lag 6  -> settled, must be dropped
        ("2026-09-09", 327.0000, "2026-09-09"),   # lag 0  -> same day, must be dropped
        ("2026-09-05", 327.5847, "2026-09-09"),   # lag 4  -> at the limit, kept
        ("2026-09-08", 999.9999, "2026-09-11"),   # later run must NOT override
    ]
    _got = opening_calls(_FakeConn(_rows))
    assert set(_got) == {"2026-09-08", "2026-09-05"}, _got
    assert abs(_got["2026-09-08"] - 327.4306) < 1e-9, _got   # earliest run wins
    print(f"snapshots self-check passed (am/pm boundary {AM_PM_BOUNDARY_HOUR}:00, "
          f"lookback {SNAPSHOT_LOOKBACK_DAYS}d, max opening lag {MAX_OPENING_LAG_DAYS}d)")
