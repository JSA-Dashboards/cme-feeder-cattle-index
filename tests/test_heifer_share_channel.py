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


def build(rows):
    """An in-memory feeder_receipts holding (week_start, channel, steers, heifers)."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE feeder_receipts (week_start TEXT, source TEXT, "
                 "channel TEXT, slug_id INT, state TEXT, steers INT, heifers INT)")
    conn.executemany(
        "INSERT INTO feeder_receipts VALUES (?, 'mars', ?, 1, 'KS', ?, ?)", rows)
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
