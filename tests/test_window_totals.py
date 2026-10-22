"""
A total that disagrees with its own column.

WHAT HAPPENED. On 2026-10-22 the deployed Index tab showed, for the 7-day
window ending Oct 20:

    Wed 10/14   3,498   805 lb   $347.66
    Thu 10/15   3,083   797 lb   $345.92
    Fri 10/16   7,207   808 lb   $337.78
    Sat 10/17-Mon 10/19  4,092   797 lb   $342.29
    Tue 10/20   1,623   814 lb   $349.80
    7-DAY TOTAL 17,538   802 lb   $342.78

The rows add to 19,503. The total said 17,538. Nothing raised, the price was
right, and the page had been doing it for as long as the table had existed.

WHY. The rows came from fci_daily's same_day_head; the total was summed from
the per-LOCATION frame, which holds no row for direct trade and video sales --
cattle that ARE in the index and belong to no auction. Two sources for one
table, and only one of them complete.

HOW IT WAS CAUGHT. Not by reading the code. By a competitor: Compass published
19,504 head and 804 lb for the same window that morning, and fci_daily's own
total_head held 19,503. Two independently derived numbers agreeing with the
ROWS and not with the TOTAL.

The property pinned below is deliberately NOT "the total matches Compass" --
that is unavailable most mornings and wrong to depend on. It is that the total
equals the sum of the rows the page displays, which a reader can check by eye,
and which is false for exactly the computation that shipped.
"""
import pytest

import composition


# The real window, as the page rendered it. Saturday and Sunday carry None:
# their sales are folded into Monday's own figures upstream.
OCT20_WINDOW = [
    (3498, 804.8),
    (3083, 797.3),
    (7207, 807.9),
    (None, None),
    (None, None),
    (4092, 796.9),
    (1623, 814.0),
]


def test_the_total_is_the_sum_of_the_rows_the_page_shows():
    head, weight = composition.window_totals(OCT20_WINDOW)
    assert head == 19503
    assert head == sum(h for h, _w in OCT20_WINDOW if h)
    assert round(weight) == 804


def test_it_agrees_with_the_two_numbers_that_caught_it():
    """
    fci_daily.total_head for 2026-10-20 was 19,503 and Compass published
    19,504 / 804 lb the same morning. Pinned as a regression anchor, not as a
    live dependency on either source.
    """
    head, weight = composition.window_totals(OCT20_WINDOW)
    assert head == 19503, "fci_daily.total_head held 19,503 for this window"
    assert abs(head - 19504) <= 2, "Compass published 19,504 for this window"
    assert abs(weight - 804) < 1, "Compass published 804 lb for this window"


def test_THE_COMPUTATION_THAT_SHIPPED_WOULD_FAIL_THIS():
    """
    THE GUARD PROVEN TO FAIL. The bug was a total summed over a population
    missing the direct-trade and video head -- here, the Friday that carries
    most of it. If the assertion above cannot tell that apart from the real
    window, it would have passed on the broken page too, and this file would
    be another check that cannot fail.
    """
    no_direct_trade = [r for r in OCT20_WINDOW if r[0] != 7207]
    head, _weight = composition.window_totals(no_direct_trade)
    assert head == 12296
    assert head != 19503
    with pytest.raises(AssertionError):
        assert head == sum(h for h, _w in OCT20_WINDOW if h)


def test_a_day_that_contributed_nothing_counts_as_zero_not_as_absent():
    """
    Skipping the empty days instead of zeroing them gives the same total here,
    so the difference only shows in the WEIGHT -- a None weight must not be
    allowed to drag the head-weighted average. Paired with the opposite input
    because that is this repo's rule for a check it intends to trust.
    """
    with_gaps = composition.window_totals([(100, 800.0), (None, None), (100, 900.0)])
    without = composition.window_totals([(100, 800.0), (100, 900.0)])
    assert with_gaps == without
    assert with_gaps[0] == 200 and round(with_gaps[1]) == 850


def test_an_empty_window_is_zero_and_None_rather_than_a_divide_by_zero():
    assert composition.window_totals([]) == (0, None)
    assert composition.window_totals([(None, None), (0, 0)]) == (0, None)


def test_the_weight_is_HEAD_weighted_not_a_flat_mean():
    """
    A flat mean of 800 and 900 is 850; weighting 900 by nine times the head
    moves it to 890. The index is pound-weighted and this column has to agree
    with it, or the table reports a draft weight no pen ever had.
    """
    _head, weight = composition.window_totals([(100, 800.0), (900, 900.0)])
    assert round(weight) == 890


@pytest.mark.parametrize("app_rel", [
    "app.py",
    "../livestock-portal/apps/cme_feeder_cattle/app.py",
])
def test_neither_app_derives_the_window_total_from_the_location_frame(app_rel):
    """
    The structural half. Both copies must take the total from the rows they
    display; reaching back into the per-location frame is what produced 17,538.
    A page-level table cannot be unit-tested here, so the ban is on the source.
    """
    import pathlib
    p = pathlib.Path(__file__).resolve().parent.parent / app_rel
    if not p.exists():
        pytest.skip("%s not checked out beside this repo" % app_rel)
    src = p.read_text(encoding="utf-8")
    assert "window_totals(" in src, "the shared helper is not being called"
    bad = [ln.strip() for ln in src.splitlines()
           if "window_total_head" in ln and "window_loc" in ln]
    assert not bad, "window total summed from the location frame again: %s" % bad
