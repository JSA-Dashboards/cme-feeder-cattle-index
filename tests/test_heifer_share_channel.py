"""
The heifer-share series reads auction receipts and no other channel.

WHY THIS IS A TEST. feeder_receipts gained a `channel` column so direct and
video receipts could be loaded from the legacy archives and tested against the
sale-barn trend. herd.py predated that column and selected every row, so the
moment those channels landed the page's series silently became a blend --
auction+direct+video through 2019, auction-only after, because the legacy
archives stop in 2020/21 and nobody has backfilled the years since.

Auction runs a mean 3.5 points above the combined figure, so the seam is a
3.5-point step sitting directly under the 2015 benchmark the page compares
today against. Nothing raised. The 2015 low simply read 38.3% instead of
42.9%, which understated how close today is to rebuild conditions by nearly
five points -- the exact quantity the page exists to report.

A magnitude check would not have caught it: every value stayed a plausible
heifer share. Only the channel filter distinguishes the two series.
"""
import sqlite3
from datetime import date, timedelta

import pytest

import herd


# herd.MIN_PANEL_STATES drops a year reported by too few states, so every
# fixture week is spread across that many. The head is duplicated per state
# rather than divided: these tests assert a RATIO, which is unchanged by the
# scaling, and an even split of 1000 across 17 states is not an integer.
STATES = ["KS", "NE", "TX", "OK", "MO", "IA", "SD", "MT", "WY", "NM",
          "AR", "TN", "KY", "VA", "NC", "GA", "AL", "MS"][:herd.MIN_PANEL_STATES]


def build(rows):
    """An in-memory feeder_receipts holding (week_start, channel, steers, heifers)."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE feeder_receipts (week_start TEXT, source TEXT, "
                 "channel TEXT, slug_id INT, state TEXT, steers INT, heifers INT)")
    conn.executemany(
        "INSERT INTO feeder_receipts VALUES (?, 'mars', ?, 1, ?, ?, ?)",
        [(w, ch, st, s, h) for (w, ch, s, h) in rows for st in STATES])
    conn.commit()
    return conn


# A full 2023 panel -- herd.MIN_YEAR_WEEKS drops any year with fewer weeks than
# this, so a single week would be silently absent rather than wrong. Auction is
# 50/50; the other channels are heifer-poor, so blending them drags the share
# down, which is precisely the corruption being excluded.
WEEKS = [date(2023, 1, 2) + timedelta(weeks=i) for i in range(herd.YTD_CUT)]


def rows(channel, steers, heifers):
    return [(w.isoformat(), channel, steers, heifers) for w in WEEKS]


AUCTION = rows("auction", 1000, 1000)
DIRECT = rows("direct", 900, 100)
VIDEO = rows("video", 900, 100)


def share(conn, year=2023):
    rows = {r["year"]: r["share"] for r in herd.heifer_share_annual(conn)}
    return rows.get(year)


def test_auction_only_is_the_series():
    assert share(build(AUCTION)) == pytest.approx(50.0)


@pytest.mark.parametrize("extra", [DIRECT, VIDEO, DIRECT + VIDEO],
                         ids=["direct", "video", "both"])
def test_other_channels_do_not_move_it(extra):
    """The number a reader sees must not depend on which channels are loaded."""
    assert share(build(AUCTION + extra)) == pytest.approx(50.0)


def test_the_blend_would_have_been_visibly_different():
    """Guards the guard: confirm the fixture actually exercises the failure."""
    conn = build(AUCTION + DIRECT + VIDEO)
    blended = conn.execute(
        "SELECT 100.0 * SUM(heifers) / (SUM(steers) + SUM(heifers)) "
        "FROM feeder_receipts").fetchone()[0]
    assert blended == pytest.approx(30.0)          # vs 50.0 auction-only
    assert share(conn) != pytest.approx(blended)


# --- coverage guard -------------------------------------------------------
#
# The auction archive reaches back to 2000 but its state coverage builds: 12
# states in 2000-01, rising to 18 by 2005. A year drawn from twelve states is a
# different survey under the same name -- not a smaller sample of the national
# share but a different mix of states, which is the quantity being measured.
# These pin the cut so a future backfill cannot quietly re-admit those years.


def test_a_thinly_covered_year_is_dropped():
    thin = [(w.isoformat(), "auction", 1000, 1000) for w in WEEKS]
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE feeder_receipts (week_start TEXT, source TEXT, "
                 "channel TEXT, slug_id INT, state TEXT, steers INT, heifers INT)")
    few = STATES[:herd.MIN_PANEL_STATES - 1]
    conn.executemany(
        "INSERT INTO feeder_receipts VALUES (?, 'mars', ?, 1, ?, ?, ?)",
        [(w, ch, st, s, h) for (w, ch, s, h) in thin for st in few])
    conn.commit()
    assert share(conn) is None            # every week present, too few states


def test_the_guard_is_states_not_head_count():
    """A low-VOLUME year with full coverage must survive.

    2015 is the live case: its head count runs ~12% under the 2011-2019 mean,
    which a head-based threshold flags as thin -- but it reports from the same
    17 states as the years either side, and the low volume IS the signal this
    page exists to report. Judging coverage by head would have deleted the
    benchmark year.
    """
    lean = [(w.isoformat(), "auction", 100, 100) for w in WEEKS]   # 10x less head
    assert share(build(lean)) == pytest.approx(50.0)


# --- the thin years are reachable, but only on purpose ---------------------


def _mixed():
    """A clean year (2023, full panel) and a thin one (2022, too few states)."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE feeder_receipts (week_start TEXT, source TEXT, "
                 "channel TEXT, slug_id INT, state TEXT, steers INT, heifers INT)")
    rows = []
    for w in WEEKS:                                   # 2023, full coverage
        rows += [(w.isoformat(), "mars", "auction", 1, st, 1000, 1000)
                 for st in STATES]
    for w in WEEKS:                                   # 2022, one state short
        rows += [(w.replace(year=2022).isoformat(), "mars", "auction", 1, st, 900, 100)
                 for st in STATES[:herd.MIN_PANEL_STATES - 1]]
    conn.executemany("INSERT INTO feeder_receipts VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()
    return conn


def test_thin_years_are_absent_from_the_main_series():
    assert [r["year"] for r in herd.heifer_share_annual(_mixed())] == [2023]


def test_thin_years_are_returned_by_the_thin_builder():
    thin = herd.heifer_share_thin(_mixed())
    assert [r["year"] for r in thin] == [2022]
    assert thin[0]["share"] == pytest.approx(10.0)


def test_the_two_series_never_overlap():
    """Whatever the cut, a year belongs to exactly one of them."""
    conn = _mixed()
    clean = {r["year"] for r in herd.heifer_share_annual(conn)}
    thin = {r["year"] for r in herd.heifer_share_thin(conn)}
    assert not (clean & thin)


def test_a_year_short_of_WEEKS_is_in_neither():
    """Incomplete is not the same as thinly covered, and gets no caveat panel."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE feeder_receipts (week_start TEXT, source TEXT, "
                 "channel TEXT, slug_id INT, state TEXT, steers INT, heifers INT)")
    conn.executemany("INSERT INTO feeder_receipts VALUES (?,?,?,?,?,?,?)",
                     [(w.isoformat(), "mars", "auction", 1, st, 1000, 1000)
                      for w in WEEKS[:herd.MIN_YEAR_WEEKS - 1] for st in STATES])
    conn.commit()
    assert herd.heifer_share_annual(conn) == []
    assert herd.heifer_share_thin(conn) == []
