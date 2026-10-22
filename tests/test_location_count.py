"""
n_locations counts BARNS, not rows.

WHAT IT WAS. recompute_fci_daily() built each window from a list of
(weight, dollars, head) tuples -- one per mars_sales row -- and set
n_locations by incrementing once per tuple. A mars_sales row is one barn in
one weight bracket, so a barn selling all four brackets counted four times.
For the window ending 2026-10-21 the column read 308 where 65 barns had
actually sold, and notify_email.py prints that figure as

    19,244 head across 308 locations

so the daily mail went out saying 308 barns had reported when 65 had.

WHY NOTHING CAUGHT IT. The number is wrong by roughly the same factor every
day, so it never jumped, never contradicted itself and never looked odd next
to yesterday's. The tuple had dropped `location` entirely, so no check inside
the function could have derived the true figure to compare against -- and the
only independent number that could, a DISTINCT count straight off mars_sales,
is what this file now takes.

The audit that found it put the cost well: a sale day where half the roster
failed to report would move the figure from 308 to about 240, "a change no
reader can interpret, because the figure is already nonsense at full
coverage".
"""
import sqlite3

import pytest

import update_index as ui

BRACKETS = (700, 750, 800, 850)


def _conn():
    c = sqlite3.connect(":memory:")
    c.execute("""CREATE TABLE mars_sales (
        report_date TEXT, raw_date TEXT, slug_id INTEGER, location TEXT,
        state TEXT, weight_low INTEGER, muscle_grade TEXT, head_count INTEGER,
        avg_weight REAL, avg_price REAL, published_date TEXT)""")
    c.execute("""CREATE TABLE fci_daily (
        report_date TEXT PRIMARY KEY, fci_value REAL NOT NULL,
        n_locations INTEGER NOT NULL, total_head INTEGER NOT NULL,
        same_day_price REAL, same_day_head INTEGER, same_day_avg_weight REAL)""")
    return c


def _sell(c, day, barn, brackets=(700,), head=50):
    for b in brackets:
        c.execute(
            "INSERT INTO mars_sales VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (day, day, 1, barn, "NE", b, "1", head, float(b + 25), 340.0, day))


def _rebuild(c):
    c.commit()
    ui.recompute_fci_daily(c)
    c.commit()
    return dict(c.execute(
        "SELECT report_date, n_locations FROM fci_daily").fetchall())


def test_ONE_BARN_IN_FOUR_BRACKETS_IS_ONE_BARN():
    """
    THE GUARD, AND THE EXACT SHAPE OF THE BUG. Four rows, one barn. The rule
    that shipped returned 4 here; if this ever reads 4 again the email is
    back to quadrupling the roster.
    """
    c = _conn()
    _sell(c, "2026-10-21", "BARN A", brackets=BRACKETS)
    got = _rebuild(c)
    c.close()
    assert got["2026-10-21"] == 1, (
        "n_locations counted rows, not barns: one barn selling %d brackets "
        "reported as %d locations" % (len(BRACKETS), got["2026-10-21"]))


def test_a_barn_selling_on_three_days_of_the_window_counts_once():
    """
    The other half of the same mistake. The window is seven days, so a barn
    with a weekly sale appears in several of them; it is still one barn.
    """
    c = _conn()
    for day in ("2026-10-19", "2026-10-20", "2026-10-21"):
        _sell(c, day, "BARN A")
    got = _rebuild(c)
    c.close()
    assert got["2026-10-21"] == 1, (
        "one barn over three days of the window counted %d times"
        % got["2026-10-21"])


def test_distinct_barns_are_counted_separately():
    """
    The control. A fix that simply returned 1 would pass both tests above and
    be just as wrong in the other direction.
    """
    c = _conn()
    _sell(c, "2026-10-21", "BARN A", brackets=BRACKETS)
    _sell(c, "2026-10-21", "BARN B", brackets=(700, 750))
    _sell(c, "2026-10-20", "BARN C")
    got = _rebuild(c)
    c.close()
    assert got["2026-10-21"] == 3, (
        "three barns across seven rows reported as %d" % got["2026-10-21"])


def test_the_count_falls_when_barns_stop_reporting():
    """
    The property the email is for. Half the roster missing has to be visible
    as half the count -- which it could not be while the figure was a
    multiple of the bracket mix.
    """
    c = _conn()
    for barn in ("A", "B", "C", "D"):
        _sell(c, "2026-10-21", "BARN " + barn, brackets=BRACKETS)
    full = _rebuild(c)["2026-10-21"]

    c2 = _conn()
    for barn in ("A", "B"):
        _sell(c2, "2026-10-21", "BARN " + barn, brackets=BRACKETS)
    half = _rebuild(c2)["2026-10-21"]
    c.close()
    c2.close()

    assert (full, half) == (4, 2), (
        "the count does not track the roster: %d barns -> %d, half of them "
        "-> %d" % (4, full, half))


def test_it_matches_a_DISTINCT_count_taken_off_the_live_database():
    """
    The independent derivation, which is this repo's rule for catching a
    number: recompute in memory from the real rows, then ask mars_sales the
    same question with SQL that shares no code with the recompute.

    Read-only on data/mars_history.db -- it is pushed to production and the
    dashboard reads it, and runs fire at 07:45 and 13:00.
    """
    import pathlib
    live = pathlib.Path(__file__).resolve().parent.parent / "data" / "mars_history.db"
    if not live.exists():
        pytest.skip("no local database")
    src = sqlite3.connect("file:%s?mode=ro" % live.as_posix(), uri=True)
    try:
        rows = src.execute(
            "SELECT report_date, raw_date, slug_id, location, state, "
            "weight_low, muscle_grade, head_count, avg_weight, avg_price, "
            "published_date FROM mars_sales").fetchall()
    finally:
        src.close()
    if not rows:
        pytest.skip("mars_sales is empty")

    c = _conn()
    c.executemany("INSERT INTO mars_sales VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    got = _rebuild(c)
    c.close()

    # The same bucketing the recompute uses, applied here by hand so the
    # comparison is not the function checking itself.
    from collections import defaultdict
    from datetime import date, timedelta
    by_bucket = defaultdict(set)
    for report_date, _raw, _slug, location, *_rest in rows:
        by_bucket[ui.shifted_bucket_date(location, report_date)].add(location)

    checked = 0
    for iso, n in got.items():
        d = date.fromisoformat(str(iso)[:10])
        expected = set()
        for i in range(7):
            expected |= by_bucket.get((d - timedelta(days=i)).isoformat(), set())
        assert n == len(expected), (
            "%s: n_locations says %d, a DISTINCT count over the same 7-day "
            "window says %d" % (iso, n, len(expected)))
        checked += 1
    assert checked > 100, "only %d dates compared -- too few to mean much" % checked
