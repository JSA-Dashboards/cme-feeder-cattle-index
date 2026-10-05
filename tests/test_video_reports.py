"""
The video PDF parser, hardened against the bug class that cost 2,162 head in
direct_reports.py on 2026-10-02.

video_reports.py had the same shape: a STICKY delivery label applied to every
row beneath the cell that set it, reset at every section header, and assigned
from cells["delivery"][0] on trust. Instrumented against the live Superior
report, that column also carries "USDA", "OK", "Oklahoma", "us", "STEERS" and
"www.ams.usda.gov/lpgmn" -- eighteen times in one document, because page
furniture lands in it at a break. Each one silently became the delivery in
force for whatever followed.

It was costing nothing on the day it was measured, because a row carrying its
own label overwrites the garbage before it matters. That is luck, not design:
the identical pattern in direct_reports.py met a continuation row with no label
of its own and dropped 2,162 head, put the published index $1.82 wrong, and
sent that number to clients.

The fix is behaviour-preserving. A/B against the pre-change parser over all 19
live reports: net head change ZERO, every report identical. It closes the hole
without moving a number, which is the only kind of change CLAUDE.md allows here
without a matching move in cme_ftp_daily.
"""
from datetime import date

import pytest

import video_reports as vr


# ---------------------------------------------------------------------------
# What may enter the sticky delivery state.
# ---------------------------------------------------------------------------

# Every one of these was observed in the delivery column of the live Superior
# report on 2026-10-05. They are not hypothetical.
LIVE_FURNITURE = ["USDA", "OK", "Oklahoma", "us", "STEERS",
                  "www.ams.usda.gov/lpgmn",
                  "https://mymarketnews.ams.usda.gov/"]

# ...and these are the real delivery values from the same document.
LIVE_DELIVERIES = ["Current", "Oct", "Nov", "Dec", "Jan", "Oct-Nov", "Nov-Dec"]


@pytest.mark.parametrize("junk", LIVE_FURNITURE)
def test_page_furniture_cannot_become_a_delivery(junk):
    assert not vr.DELIVERY_RE.match(junk), (
        f"{junk!r} was seen in the delivery column of a live report; accepting "
        f"it as a delivery is what makes rows disappear downstream")


@pytest.mark.parametrize("real", LIVE_DELIVERIES)
def test_real_deliveries_are_still_accepted(real):
    """
    The other direction, and the one that would cause silent loss if the
    validator were too strict: a rejected REAL delivery leaves cur_timing stale
    or unset, which drops cattle just as effectively.
    """
    assert vr.DELIVERY_RE.match(real), f"{real!r} is a real delivery value"


def test_a_month_range_is_a_delivery_but_a_sentence_is_not():
    """Guard the guard: the pattern must be anchored at both ends."""
    assert vr.DELIVERY_RE.match("Oct-Nov")
    assert not vr.DELIVERY_RE.match("Oct Nov")        # space, not a range
    assert not vr.DELIVERY_RE.match("Current delivery issues")
    assert not vr.DELIVERY_RE.match("xOct")
    assert not vr.DELIVERY_RE.match("Octx")


# ---------------------------------------------------------------------------
# The name collision, which broke every video report.
# ---------------------------------------------------------------------------

def test_months_is_still_the_published_date_lookup():
    """
    THE REGRESSION THIS FILE EXISTS FOR AS MUCH AS THE PARSER.

    This module already had _MONTHS -- a {name: number} dict that
    _parse_published_date() indexes by month NAME. The first version of the
    delivery validator defined its own _MONTHS as a tuple of abbreviations and
    shadowed it, so date(year, _MONTHS[month], day) raised "tuple indices must
    be integers or slices, not str" and EVERY video report skipped. The whole
    video ingest, lost to a name collision.

    No unit test caught it. It surfaced only on running the live path, because
    fetch_all_video_rows catches per-report exceptions and prints "[skip]" --
    so the failure looked like nineteen unavailable reports rather than a crash.
    """
    assert isinstance(vr._MONTHS, dict), \
        "_MONTHS is the published-date lookup; something shadowed it"
    assert vr._MONTHS.get("January") == 1 or vr._MONTHS.get("Jan") == 1, \
        "_MONTHS must still map a month NAME to its number"
    assert all(isinstance(v, int) for v in vr._MONTHS.values())


def test_the_published_date_parser_still_works():
    """
    The function the collision broke, exercised end to end rather than by
    inspecting its lookup table -- an assertion about _MONTHS alone would pass
    against a dict that had the wrong keys.
    """
    got = vr._parse_published_date(
        "USDA Market News    Oklahoma City, OK    October 1, 2026    "
        "FEEDER CATTLE")
    assert got == date(2026, 10, 1), got
    # and the restriction that keeps a narrative date out of it
    far = "x" * 600 + " September 17, 2026"
    assert vr._parse_published_date(far) is None, \
        "a date past the head of the page is narrative, not the stamp"


# ---------------------------------------------------------------------------
# The seams: both parsers and the collector must agree on one shape.
# ---------------------------------------------------------------------------

def test_both_parsers_return_the_completeness_channel():
    """
    parse_western_video_pdf has no sticky delivery of this kind and always
    returns an empty list, deliberately -- so fetch_all_video_rows needs no
    special case, and a special case is where the next one of these hides.
    """
    import inspect
    for fn in (vr.parse_video_pdf, vr.parse_western_video_pdf):
        src = inspect.getsource(fn)
        assert "return report_date, rows, " in src, \
            f"{fn.__name__} no longer returns the unlabelled channel"


def test_the_collector_carries_the_channel_to_update_index():
    import inspect
    src = inspect.getsource(vr.fetch_all_video_rows)
    assert "report_date, rows, unlabelled = parser(pdf_bytes)" in src
    assert "out[name] = (report_date, published_date, rows, unlabelled)" in src, \
        "update_index.py destructures four values; the collector must supply them"


def test_update_index_destructures_four_values():
    """
    The seam that would fail at 07:45 rather than here. update_index.py unpacks
    the per-report tuple positionally, so a collector that went back to three
    would raise mid-run with the index half built.
    """
    from pathlib import Path
    src = (Path(vr.__file__).resolve().parent / "update_index.py").read_text(
        encoding="utf-8")
    assert "for name, (report_date_, published_date_, rows, unlabelled_) in video_results.items():" in src, \
        "update_index.py and fetch_all_video_rows disagree about the tuple shape"
