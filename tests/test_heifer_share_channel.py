"""
The heifer-share series reads auction, direct and video, one source per channel.

WHY THIS FILE EXISTS. It was written when the series was auction-only, because
feeder_receipts had gained a `channel` column and herd.py predated it: loading
direct and video silently turned the series into a blend, three channels through
2019 and auction alone after, with a 2-4 point step at the seam sitting under the
2015 benchmark. Nothing raised; every value stayed a plausible heifer share.

The series is now deliberately all-channel (2026-09-29), because MARS turned out
to serve direct and video after all and the gap could be closed at source. So
these no longer pin "auction only" -- they pin the two things that make an
all-channel series correct rather than merely wider:

  * ONE SOURCE PER CHANNEL PER WEEK. The legacy and MARS archives overlap on 26
    auction weeks and 19 video weeks. Summing them double-counts. That was
    written wrong twice in one session and neither time did anything raise.
  * ALL CHANNELS OR NONE, per week. The channels sit ~10 points apart, so a week
    carrying two of three is not a smaller sample of the national mix but a
    different one.
"""
import sqlite3
from datetime import date, timedelta

import pytest

import herd

COLS = "(week_start, source, channel, slug_id, state, steers, heifers)"

# herd.MIN_PANEL_STATES drops a year reported by too few states, so every fixture
# week is spread across that many. Head is duplicated per state rather than
# divided: these assert RATIOS, which the scaling leaves alone.
STATES = ["KS", "NE", "TX", "OK", "MO", "IA", "SD", "MT", "WY", "NM",
          "AR", "TN", "KY", "VA", "NC", "GA", "AL", "MS"][:herd.MIN_PANEL_STATES]

# 2023: past every channel's handover, so "mars" is the owning source.
WEEKS = [date(2023, 1, 2) + timedelta(weeks=i) for i in range(herd.YTD_CUT)]


def build(rows):
    """rows: (week, source, channel, steers, heifers) -> an in-memory table."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE feeder_receipts (week_start TEXT, source TEXT, "
                 "channel TEXT, slug_id INT, state TEXT, steers INT, heifers INT)")
    conn.executemany(
        f"INSERT INTO feeder_receipts {COLS} VALUES (?,?,?,1,?,?,?)",
        [(w, so, ch, st, s, h) for (w, so, ch, s, h) in rows for st in STATES])
    conn.commit()
    return conn


def every_week(source, channel, steers, heifers):
    return [(w.isoformat(), source, channel, steers, heifers) for w in WEEKS]


def share(conn, year=2023):
    return {r["year"]: r["share"] for r in herd.heifer_share_annual(conn)}.get(year)


# A national mix with the channels far apart, the way they really are.
BALANCED = (every_week("mars", "auction", 1000, 1000)      # 50%
            + every_week("mars", "direct", 900, 100)       # 10%
            + every_week("mars", "video", 900, 100))       # 10%


def test_every_channel_contributes():
    """2800 steers + 1200 heifers across the three channels = 30%."""
    assert share(build(BALANCED)) == pytest.approx(30.0)


@pytest.mark.parametrize("missing", ["direct", "video", "auction"])
def test_a_week_missing_one_channel_is_skipped(missing):
    """Not a smaller sample of the mix -- a different mix. Dropping the year is
    the honest outcome, not quietly reporting the other two as if national."""
    rows = [r for r in BALANCED if r[2] != missing]
    assert share(build(rows)) is None


def test_sources_are_picked_not_summed():
    """THE DOUBLE-COUNT TEST.

    Legacy and MARS overlap on real weeks. Summed, the head doubles and the
    share drifts toward whichever archive is heifer-richer. 2023 is past the
    handover, so only the MARS rows may count.
    """
    decoy = every_week("legacy", "auction", 100, 1900)     # heifer-rich, must be ignored
    assert share(build(BALANCED + decoy)) == pytest.approx(30.0)


def test_legacy_owns_weeks_before_the_handover():
    """The mirror image: before the boundary the legacy rows are the ones counted."""
    old = [date(2015, 1, 5) + timedelta(weeks=i) for i in range(herd.YTD_CUT)]
    rows = []
    for w in old:
        for c, s, h in (("auction", 1000, 1000), ("direct", 900, 100),
                        ("video", 900, 100)):
            rows.append((w.isoformat(), "legacy", c, s, h))
        rows.append((w.isoformat(), "mars", "auction", 100, 1900))   # decoy
    assert share(build(rows), 2015) == pytest.approx(30.0)


def test_2020_is_excluded_entirely():
    """MARS direct starts at week 39, past the week-37 basis, so 2020 direct
    would be legacy-only and legacy is a decaying remnant by then."""
    w2020 = [date(2020, 1, 6) + timedelta(weeks=i) for i in range(herd.YTD_CUT)]
    rows = []
    for w in w2020:
        for c, s, h in (("auction", 1000, 1000), ("direct", 900, 100),
                        ("video", 900, 100)):
            rows.append((w.isoformat(), "legacy", c, s, h))
    conn = build(rows)
    assert 2020 not in {r["year"] for r in herd.heifer_share_annual(conn)}
    assert 2020 not in {r["year"] for r in herd.heifer_share_thin(conn)}


def test_the_coverage_guard_counts_auction_states_only():
    """The guard exists because the AUCTION archive's panel builds over the early
    years. Direct and video have their own, much narrower geography -- counting
    all three would let a year pass on their coverage while the auction panel
    behind most of its head was still a third missing.
    """
    few = STATES[:herd.MIN_PANEL_STATES - 1]
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE feeder_receipts (week_start TEXT, source TEXT, "
                 "channel TEXT, slug_id INT, state TEXT, steers INT, heifers INT)")
    rows = []
    for (w, so, ch, s, h) in BALANCED:
        for st in (few if ch == "auction" else STATES):
            rows.append((w, so, ch, st, s, h))
    conn.executemany(f"INSERT INTO feeder_receipts {COLS} VALUES (?,?,?,1,?,?,?)", rows)
    conn.commit()
    assert share(conn) is None            # thin auction disqualifies the year


def test_thin_and_clean_series_never_overlap():
    conn = build(BALANCED)
    clean = {r["year"] for r in herd.heifer_share_annual(conn)}
    thin = {r["year"] for r in herd.heifer_share_thin(conn)}
    assert not (clean & thin)


def test_the_guard_is_states_not_head_count():
    """A low-VOLUME year with full coverage must survive.

    2015 is the live case: its head runs well under the surrounding mean, which
    a head-based threshold flags as thin -- but it reports from the same states
    as its neighbours and the low volume IS the signal. Judging coverage by head
    would have deleted the benchmark year.
    """
    lean = [(w, so, ch, s // 10, h // 10) for (w, so, ch, s, h) in BALANCED]
    assert share(build(lean)) == pytest.approx(30.0)


def test_a_short_year_lands_in_the_caveated_segment_not_nowhere():
    """Between MIN_THIN_WEEKS and MIN_YEAR_WEEKS a year is shown apart, not dropped.

    2002-2004 are the live case: the all-channel rule (every channel or skip the
    week) put them at 27-29 weeks because video covered no more, and they
    vanished from a chart that had shown them. Their measured seasonal bias is
    under a third of a point, so vanishing was the wrong answer.
    """
    short = [w for w in WEEKS[:herd.MIN_YEAR_WEEKS - 1]]      # enough for thin, not clean
    rows = []
    for w in short:
        for c, s, h in (("auction", 1000, 1000), ("direct", 900, 100),
                        ("video", 900, 100)):
            rows.append((w.isoformat(), "mars", c, s, h))
    conn = build(rows)
    assert share(conn) is None                                # not in the clean series
    assert 2023 in {r["year"] for r in herd.heifer_share_thin(conn)}


def test_a_year_below_even_the_thin_bar_is_dropped():
    """Below MIN_THIN_WEEKS nothing has been measured, so nothing is claimed."""
    tiny = WEEKS[:herd.MIN_THIN_WEEKS - 1]
    rows = []
    for w in tiny:
        for c, s, h in (("auction", 1000, 1000), ("direct", 900, 100),
                        ("video", 900, 100)):
            rows.append((w.isoformat(), "mars", c, s, h))
    conn = build(rows)
    assert herd.heifer_share_annual(conn) == []
    assert herd.heifer_share_thin(conn) == []


def test_video_hands_over_at_week_19_not_18():
    """The boundary week belongs to legacy, and getting it wrong drops the week.

    MARS video's first week is 2020-05-04 = ISO 2020W19, so giving that week to
    MARS looked right. In it legacy carries 28,687 head and MARS 1,967, because
    MARS is starting up rather than legacy finishing -- the old boundary threw
    away 26,720 head. Nothing published moved (2020 is in SKIP_YEARS), which is
    exactly why it needs a test rather than a reader noticing.
    """
    assert herd.CHANNEL_LEGACY_THROUGH["video"] == (2020, 19)


def test_the_boundary_prefers_the_source_that_actually_covers_the_week():
    """A ramping-up MARS week must not displace a full legacy one, and vice versa."""
    w = date.fromisocalendar(2020, 19, 1)
    rows = []
    for c, s, h in (("auction", 1000, 1000), ("direct", 900, 100)):
        rows.append((w.isoformat(), "legacy", c, s, h))
    rows.append((w.isoformat(), "legacy", "video", 20000, 8687))   # full legacy week
    rows.append((w.isoformat(), "mars", "video", 1400, 567))       # MARS ramping up
    conn = build(rows)
    picked = herd._feeder_weeks(conn)[(2020, 19)]["video"]
    assert picked == [20000 * len(STATES), 8687 * len(STATES)]
