"""
cme_ftp.py's bracket path: slice before tokenizing, and prove the brackets sum.

WHAT WAS WRONG. parse_daily_file tried the whitespace-token path first and only
fell back to slicing by the file's own header columns. A mis-split that happens
to preserve the token count passes that path silently, and one does: on
2026-09-30 El Reno's 750 bracket read

    761.08341   9.0            where the file states   761.08   341.90

The space moved, two tokens stayed two tokens, len(toks) == 29 still passed,
and the bracket landed with a weight wrong by 0.003 lb and a price of $9.00.
Ten rows in 72,822 carry it.

MATERIALITY, stated honestly: ZERO cents on the published index. cme_ftp feeds
the independent comparison, not the index -- recompute_fci_daily() builds the
index from mars_sales. Within cme_ftp, reported_index and the seven-day totals
come from the TOTALS branch and were never touched, cme_ftp_locations comes
from the row tail and was never touched, and the only corrupted field is
cme_ftp_brackets.avg_price, whose sole consumer in the repo -- composition.py's
baseline_grade_share() -- reads head and weight and never price.

So this is fixed for the mechanism, not the money. len(toks) == 29 was the ONLY
validity check on that path, and it is the same shape of blind spot as the
2026-09-14 layout change, which was found days late by a human noticing the
index had stopped moving.
"""
import datetime as dt

import pytest

import cme_ftp


# One real location row from the 2026-09-30 file, with the header above it.
# Kept as text rather than a fixture file because the whole point is the
# column geometry, and a 400 KB binary would hide it.
HEADER = (
    "Sale Date Sale Name        Head Weight Price Head Weight Price Head Weight "
    "Price Head Weight Price Head Weight Price Head Weight Price Head Weight "
    "Price Head Weight Price Head Weight Price Weight"
)


def _layout_from(header_line):
    return cme_ftp.header_layout([header_line])


def test_header_layout_reads_twenty_four_bracket_spans():
    lay = _layout_from(HEADER)
    assert lay is not None, "the header must yield a layout"
    spans, tail_at = lay
    assert len(spans) == 24
    # Spans are consecutive header positions, so the tail begins exactly where
    # the last bracket span ends. Asserting ">" was wrong and said nothing.
    assert tail_at == spans[-1][1]
    assert all(a < b for a, b in spans), "spans must advance"


def test_header_layout_is_none_without_a_header():
    assert cme_ftp.header_layout(["no columns here"]) is None
    assert cme_ftp.header_layout([]) is None


def test_the_slice_path_is_tried_before_the_token_path():
    """
    Order is the fix. Both branches exist and both parse; the token one is a
    fallback for pre-2021 files that have no header to slice by. If the token
    branch is ever moved back in front, the glued-value bug returns and nothing
    else in this suite notices.
    """
    import inspect
    src = inspect.getsource(cme_ftp.parse_daily_file)
    slice_at = src.index("elif layout and not is_totals")
    token_at = src.index("elif len(toks) == 29")
    assert slice_at < token_at, (
        "the token path must stay BELOW the slice path -- a mis-split that "
        "preserves the token count passes it silently")


# ---------------------------------------------------------------------------
# The arithmetic guard.
# ---------------------------------------------------------------------------

def _parsed(locations):
    return {"date": "2026-09-30", "locations": locations}


def _loc(head, total_lb, brackets):
    return {"location": "Test", "head": head, "total_lb": total_lb,
            "brackets": brackets}


def _b(head, wt, price=300.0):
    return {"grade": "1", "weight_low": 750, "head": head,
            "avg_weight": wt, "avg_price": price}


def test_a_clean_row_is_silent():
    p = _parsed([_loc(100, 75000.0, [_b(60, 750.0), _b(40, 750.0)])])
    assert cme_ftp.bracket_anomalies(p) == []


def test_a_dropped_bracket_fires():
    """Brackets short of the row's own stated head."""
    p = _parsed([_loc(100, 75000.0, [_b(60, 750.0)])])
    bad = cme_ftp.bracket_anomalies(p)
    assert len(bad) == 1
    assert bad[0]["stated_head"] == 100 and bad[0]["bracket_head"] == 60


def test_the_guard_does_NOT_catch_the_glued_digit_bug():
    """
    THE LIMIT OF THIS GUARD, asserted so nobody mistakes it for cover.

    The first version of this test claimed the arithmetic catches the
    2026-09-30 El Reno row. It does not, and the numbers say why: the glued
    digits land after the decimal point, so 761.08341 against 761.08 is 0.24 lb
    on 53,275 -- a relative error of 0.0000045, four hundred times under the
    0.0005 tolerance and well inside CME's own rounding.

    No tolerance can separate that from rounding without firing on every clean
    row. The SLICE-FIRST ORDERING is what fixes this bug; the guard is for a
    different class -- a layout change that drops or misaligns whole values,
    which is what happened on 2026-09-14. Keeping both, and being clear about
    which does what, is the point.
    """
    p = _parsed([_loc(70, 53275.6, [_b(70, 761.08341)])])
    assert cme_ftp.bracket_anomalies(p) == [], (
        "if this starts firing the tolerance has been tightened to where it "
        "will also fire on ordinary rounding -- check that before celebrating")


def test_rows_without_brackets_are_not_judged():
    """
    Pre-2021 files have no header, so no brackets. Absence is not a
    discrepancy, and reporting it would print on every historical backfill --
    which is how a check stops being read.
    """
    assert cme_ftp.bracket_anomalies(_parsed([_loc(100, 75000.0, [])])) == []
    assert cme_ftp.bracket_anomalies({"date": "x", "locations": []}) == []


def test_a_zero_head_row_is_not_judged():
    assert cme_ftp.bracket_anomalies(
        _parsed([_loc(0, 0.0, [_b(5, 750.0)])])) == []


def test_the_tolerance_admits_rounding_but_not_a_real_error():
    """
    CME prints weights to two decimals, so the brackets reconstruct the row's
    pounds to within rounding and no further. Guard both sides of the
    threshold, or the tolerance is just a number nobody checked.
    """
    # 0.02% out -- rounding, must stay silent
    assert cme_ftp.bracket_anomalies(
        _parsed([_loc(100, 75000.0, [_b(100, 750.15)])])) == []
    # 0.5% out -- real, must fire
    assert cme_ftp.bracket_anomalies(
        _parsed([_loc(100, 75000.0, [_b(100, 753.75)])])) != []
