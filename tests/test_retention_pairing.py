"""
The retention incentive divides a bred female by HER OWN BARN's cull cows.

WHY THIS FILE EXISTS. retention_incentive() keyed its accumulator on
report_date alone, with no barn in it, so the numerator and the denominator
were head-weighted averages over whatever markets happened to report that day.
That is not a detail: five of the twelve slugs in REPLACEMENT_SLUGS are
Replacement Specials carrying bred females and NO slaughter side at all --
179,481 of 354,109 bred head, 50.7% -- so half the bred head on the page was
priced against some other state's cull cows. The page's own words are "sell her
bred to a neighbour, or ship her to the packer": one animal, one market.

Nothing raised, and nothing could. A ratio of a plausible bred price over a
plausible cull price is a plausible ratio whichever barns they came from, and
the published series moved by at most 0.031 when the pairing was corrected --
small enough to look like nothing and wrong for a reason no reader could see.

AND SAME-BARN IS NOT SAME-DATE, which is the trap inside the fix. A Replacement
Special is not held on sale day: Billings, Tina and the Joplin special pair on
the exact date ZERO times out of 90. A strict same-barn-same-date rule would
therefore have DELETED the very bred head it was meant to rescue, leaving a
chart that looked tidier and covered half the market. Hence nearest-within-
MAX_PAIR_GAP_DAYS, and hence the tests below that pin both halves.
"""
import sqlite3
from datetime import date, timedelta

import pytest

import herd
import replacement_reports

COLS = ("report_date, slug_id, commodity, class_desc, price_unit, "
        "head_count, avg_weight, avg_price, age, receipts, receipts_year_ago")


def build(rows):
    """rows: (iso_date, slug, kind, head, weight, price) -> in-memory table.

    kind is "bred" or "salv"; the commodity/class/unit triplet each one needs is
    filled in here so a test reads as the market event it represents.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE replacement_sales (report_date TEXT, slug_id INT, "
        "commodity TEXT, class_desc TEXT, price_unit TEXT, head_count INT, "
        "avg_weight REAL, avg_price REAL, age TEXT, receipts INT, "
        "receipts_year_ago INT)")
    for iso, slug, kind, head, wt, price in rows:
        if kind == "bred":
            trio = ("Replacement Cattle", "Bred Cows", "Per Unit")
        else:
            trio = ("Slaughter Cattle", "Cows", "Per Cwt")
        conn.execute(
            f"INSERT INTO replacement_sales ({COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (iso, slug, trio[0], trio[1], trio[2], head, wt, price, None, None, None))
    conn.commit()
    return conn


# Two barns in BARN_OF_SLUG that are definitely different markets.
JOPLIN, JOPLIN_SPECIAL = 1797, 1798
BILLINGS_SPECIAL, BILLINGS_AUCTION = 2257, 1774
ADA = 1843


def test_bred_is_not_divided_by_another_barns_cull_cows():
    """The regression this file exists for.

    Ada sells bred females against cheap cull cows; Joplin sells none that day
    but prints an expensive cull. Pooling on the date alone divides Ada's bred
    females by the blend and reports a ratio no animal ever traded at.
    """
    conn = build([
        ("2026-03-03", ADA,    "bred", 10, 1200, 2000.0),
        ("2026-03-03", ADA,    "salv", 10, 1200,  100.0),   # $1,200/head salvage
        ("2026-03-03", JOPLIN, "salv", 90, 1200,  200.0),   # $2,400/head, 9x the head
    ])
    rows = herd.retention_incentive(conn)
    ada = [r for r in rows if r["barn"] == "Ada, OK"]
    assert len(ada) == 1
    # Ada's own salvage, not the head-weighted blend of 2,280 that pooling gave.
    assert ada[0]["salvage"] == pytest.approx(1200.0)
    assert ada[0]["ratio"] == pytest.approx(2000.0 / 1200.0)
    # And Joplin contributes no row at all: it sold no bred females that day.
    assert not [r for r in rows if r["barn"] == "Joplin, MO"]


def test_replacement_special_pairs_to_its_own_barns_auction():
    """A special held two days off sale day still belongs to that market.

    This is the whole reason the rule is nearest-within-a-window. Billings'
    bred females come from slug 2257 and its cull cows from 1774, on different
    days; a same-date rule drops 88,784 head of real trade.
    """
    conn = build([
        ("2026-03-05", BILLINGS_SPECIAL, "bred", 20, 1300, 2600.0),
        ("2026-03-03", BILLINGS_AUCTION, "salv", 40, 1300,  100.0),
    ])
    rows = herd.retention_incentive(conn)
    assert len(rows) == 1
    assert rows[0]["barn"] == "Billings, MT"
    assert rows[0]["gap_days"] == 2
    assert rows[0]["ratio"] == pytest.approx(2600.0 / 1300.0)


def test_a_salvage_print_beyond_the_window_is_refused_not_stretched():
    conn = build([
        ("2026-03-20", BILLINGS_SPECIAL, "bred", 20, 1300, 2600.0),
        ("2026-03-03", BILLINGS_AUCTION, "salv", 40, 1300,  100.0),
    ])
    assert herd.retention_incentive(conn) == []


def test_ties_break_to_the_earlier_cull_print():
    """Equidistant before and after, the earlier one wins.

    A cull price printed before the bred sale was information the buyer had
    standing in the ring. One printed after it was not.
    """
    conn = build([
        ("2026-03-10", BILLINGS_SPECIAL, "bred", 10, 1000, 1500.0),
        ("2026-03-08", BILLINGS_AUCTION, "salv", 10, 1000,  100.0),   # $1,000
        ("2026-03-12", BILLINGS_AUCTION, "salv", 10, 1000,  150.0),   # $1,500
    ])
    rows = herd.retention_incentive(conn)
    assert len(rows) == 1
    assert rows[0]["salvage"] == pytest.approx(1000.0)


def test_a_barn_with_no_cull_side_anywhere_contributes_nothing():
    """Salina reports bred females and no cull cows, and has no companion sale.

    It must drop out rather than borrow a denominator -- which is exactly what
    the old code did for it on every date another barn reported.
    """
    salina = next(s for s, b in herd.BARN_OF_SLUG.items()
                  if b in herd.UNPAIRABLE_BARNS)
    conn = build([
        ("2026-03-03", salina, "bred", 50, 1200, 2400.0),
        ("2026-03-03", ADA,    "salv", 50, 1200,  100.0),
    ])
    assert herd.retention_incentive(conn) == []


def test_the_special_and_the_regular_sale_are_one_barn():
    """Joplin 1797 and 1798 are the same yard and must share a denominator."""
    assert herd.BARN_OF_SLUG[JOPLIN] == herd.BARN_OF_SLUG[JOPLIN_SPECIAL]
    assert herd.BARN_OF_SLUG[BILLINGS_SPECIAL] == herd.BARN_OF_SLUG[BILLINGS_AUCTION]


def test_every_ingested_slug_has_a_barn():
    """A slug the ingest stores but BARN_OF_SLUG does not name is dropped.

    retention_incentive() skips any row whose slug is unmapped, so adding a slug
    to REPLACEMENT_SLUGS without adding it here removes that market from the
    ratio silently -- the page keeps rendering, with less data behind it.
    """
    missing = set(replacement_reports.REPLACEMENT_SLUGS) - set(herd.BARN_OF_SLUG)
    assert not missing, f"slugs ingested but not mapped to a barn: {sorted(missing)}"


def test_a_stub_year_is_not_drawn_as_a_year():
    """The MARS floor leaves 2018 with three months at one barn.

    Those 13 observations median to 1.61, which would print as the tallest bar
    on the chart under a label that says 2018.
    """
    rows = []
    for i in range(3):                                   # Oct-Dec only
        d = date(2018, 10, 1) + timedelta(days=30 * i)
        rows += [(d.isoformat(), ADA, "bred", 10, 1200, 2000.0),
                 (d.isoformat(), ADA, "salv", 10, 1200,  100.0)]
    for i in range(12):                                  # a full year beside it
        d = date(2019, 1, 15) + timedelta(days=30 * i)
        rows += [(d.isoformat(), ADA, "bred", 10, 1200, 1500.0),
                 (d.isoformat(), ADA, "salv", 10, 1200,  100.0)]
    conn = build(rows)
    years = [a[0] for a in herd.annual_ratio(conn)]
    assert "2018" not in years
    assert "2019" in years


def test_annual_span_marks_a_part_year_and_leaves_a_whole_one_alone():
    def year_of(n_months):
        return [((date(2026, 1, 15) + timedelta(days=30 * i)).isoformat(),
                 ADA, k, 10, 1200, 2000.0 if k == "bred" else 100.0)
                for i in range(n_months) for k in ("bred", "salv")]

    assert herd.annual_span(build(year_of(10)), 2026) == ("Jan", "Oct")
    assert herd.annual_span(build(year_of(12)), 2026) is None
