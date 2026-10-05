"""
The direct/video PDF parser, against the report that broke it.

WHAT HAPPENED. On 2026-10-02 the Texas Direct report put 2,162 head of
800-849 lb Medium & Large 1-2 steers at $322.79 on page 2, and the parser
dropped every one. Our index printed 339.55; CIH printed 337.76; the true
figure is 337.74. The wrong number went to clients before anyone noticed, and
nothing anywhere went red -- the daily job exited 0, the barn report said the
day was whole, and TX DIRECT was PRESENT in the data at 1,779 head, which is
45% of its real size. A barn that reports short looks exactly like a barn that
reported.

TWO BUGS, COMPOUNDING, both about the same sticky state. cur_timing and
cur_freight carry a "Delivery/Freight" label down the weight rows beneath it,
so anything that corrupts them silently drops cattle:

  1. PAGE FURNITURE lands in the freight column. "USDA AMS Livestock, Poultry
     & Grain Market News" and "Email us with accessibility issues with this
     report." were both read as labels, setting the basis to "AMS" and "us".

  2. A SECTION HEADER REPEATED at the top of a page is a continuation, not a
     new group, and its first rows inherit the label from the previous page.
     Resetting the state there left them with no label at all.

Either alone is survivable; together they turned a 2,162-head row into nothing.

THE FIXTURE IS THE REAL REPORT, 396 KB, trimmed to the two pages carrying the
Steers sections. CLAUDE.md says not to add tracked binaries and this is a
deliberate exception: the bug is in how the parser walks a real multi-page
layout, and a synthetic PDF would prove only that the synthetic one parses.
"""
import gzip
from datetime import date
from pathlib import Path

import pytest

import direct_reports as dr

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "tx_direct_2026-10-02.pdf.gz"

pytestmark = pytest.mark.skipif(not FIXTURE.exists(),
                                reason="TX direct fixture not checked out")


def tx_pdf():
    return gzip.decompress(FIXTURE.read_bytes())


def parsed():
    return dr.parse_direct_pdf(tx_pdf(), "TX")


def test_the_whole_report_is_read():
    """
    3,941 head, which is what CIH published for Texas Direct that day and what
    the index needs. 1,779 is the broken answer and the one that shipped.
    """
    report_date, rows = parsed()
    assert report_date == date(2026, 10, 2)
    assert sum(r["head_count"] for r in rows) == 3941, \
        "TX Direct 2026-10-02 is 3,941 qualifying head; 1,779 is the page-2 bug"


def test_the_page_two_continuation_row_survives():
    """
    The row itself, named so a future regression says which one went missing.
    It is a continuation of the Steers 1-2 Current FOB group whose label sits
    on page 1.
    """
    _d, rows = parsed()
    hit = [r for r in rows if r["head_count"] == 2162]
    assert hit, "the 2,162 head page-2 row is missing again"
    r = hit[0]
    assert r["weight_break_low"] == 800
    assert r["muscle_grade"] == "1-2"
    assert r["avg_weight"] == pytest.approx(820.0)
    assert r["avg_price"] == pytest.approx(322.79)


def test_the_pound_weighted_price_matches_what_cih_published():
    """
    An independently published number, which is the only kind this project
    trusts. CIH printed 3,941 head at $326.10 for Texas Direct on 2026-10-02.
    """
    _d, rows = parsed()
    lb = sum(r["head_count"] * r["avg_weight"] for r in rows)
    px = sum(r["head_count"] * r["avg_weight"] * r["avg_price"] for r in rows) / lb
    assert px == pytest.approx(326.10, abs=0.005)


def test_only_current_fob_is_taken():
    """
    The filter that defines the sample, and the one the fixes must not have
    loosened. This report carries Current DEL, Oct DEL, Nov FOB, Nov DEL,
    Dec DEL, "Oct - Nov FOB" and May FOB rows inside the index weight band;
    none of them belong in a cash index and none may appear here.

    Dec DEL 580 head at 850 lb and Nov DEL 293 at 850 are the two that would
    show up first, so assert against their exact head counts.
    """
    _d, rows = parsed()
    heads = {r["head_count"] for r in rows}
    for forbidden, what in ((580, "Dec DEL"), (293, "Nov DEL"), (240, "Oct DEL"),
                            (210, "Current DEL"), (65, "Nov FOB")):
        assert forbidden not in heads, f"{what} row leaked into the sample"
    assert heads == {80, 214, 525, 2162, 960}


def test_a_multi_token_timing_keeps_its_real_basis():
    """
    "Oct - Nov FOB" is a four-token label whose basis is FOB, not "-". The old
    code read token[1] as the basis, which excluded this row for the WRONG
    reason -- it would have been included the moment AMS dropped the dash.

    The 525-head 800 lb row at 327.58 is that label in this report, and it must
    stay out while the 525-head 772 lb Current FOB row stays in.
    """
    _d, rows = parsed()
    r800 = [r for r in rows if r["head_count"] == 525]
    assert len(r800) == 1
    assert r800[0]["avg_weight"] == pytest.approx(772.0), \
        "the Oct - Nov FOB 800 lb row was taken instead of the Current FOB one"


def test_page_furniture_is_not_read_as_a_freight_label():
    """
    Guard the guard, at the unit the bug lived in. These strings really appear
    in the freight column of this report.
    """
    label = lambda s: (len(s.split()) >= 2
                       and s.split()[0] in dr.TIMINGS
                       and s.split()[-1] in dr.FREIGHT_BASES)
    for junk in ("USDA AMS Livestock, Poultry & Grain Market News",
                 "Email us with accessibility issues with this report.",
                 "General inquiries, please call: (202) 720-1990",
                 "Dairy Steers - Large 3 (Per Cwt)",
                 "Texas Direct Cattle Report"):
        assert not label(junk), f"page furniture read as a label: {junk}"
    for real in ("Current FOB", "Current DEL", "Oct DEL", "Oct - Nov FOB",
                 "May FOB", "Dec DEL"):
        assert label(real), f"a real label was rejected: {real}"


def test_the_fixture_actually_exercises_the_page_break():
    """
    If the fixture is ever replaced by a one-page report, every test above
    passes while proving nothing about the bug. Assert the shape that made it.
    """
    import io
    import pdfplumber
    with pdfplumber.open(io.BytesIO(tx_pdf())) as pdf:
        assert len(pdf.pages) >= 2, "the fixture must span a page break"
        page_two = pdf.pages[1].extract_text() or ""
    assert "Steers" in page_two, "page 2 must carry a Steers section"
