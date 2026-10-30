"""
archive_history.py: the restated archive arranged for the page.

THE RISK IS THE SAME ONE THE TABLES WERE BUILT AROUND -- a restated weighted
average being read as, or joined to, the index CME actually printed. The
module's defence is that nothing in it returns a blended series, and the first
test here asserts that structurally rather than taking the docstring's word.

OFFLINE. Every test drives a fake connection; nothing opens SQLite or reaches
Snowflake, so the suite runs with no credentials and off the index's critical
path. Both backends are modelled: SQLite hands back ISO strings and the
Snowflake connector hands back datetime.date, which is exactly the difference
db.iso() exists to absorb, so the fakes return one of each.
"""

import ast
import re
import sys
from datetime import date
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
for candidate in (HERE.parent / "apps" / "cme_feeder_cattle", HERE.parent):
    if (candidate / "archive_history.py").exists():
        APP_DIR = candidate
        break
else:                                                    # pragma: no cover
    raise RuntimeError("archive_history.py not found in either layout")
sys.path.insert(0, str(APP_DIR))

import archive_history as ah  # noqa: E402


class FakeCursor:
    def __init__(self, plans):
        self._plans = plans
        self._rows = []

    def execute(self, sql, *_a):
        for needle, project in self._plans:
            if needle in sql:
                self._rows = project()
                return self
        raise AssertionError("unexpected query: %s" % sql)

    def fetchall(self):
        return self._rows


class FakeConn:
    """
    Enough of a DB-API connection for this module's reads.

    EACH TABLE IS READ AT TWO DIFFERENT WIDTHS -- load_series wants one or two
    columns, agreement_by_year wants three -- so the fake projects to match
    the SELECT rather than handing the same tuple to both. A fake that always
    returned the wide row would let a function unpack columns it never asked
    for, which is the sort of latitude that makes a green suite worthless.
    Most specific needle first.
    """

    def __init__(self, archive, printed):
        # Canonical rows: archive (date, price, head); printed (date, value, head).
        self._plans = [
            ("seven_day_head", lambda: [tuple(r) for r in archive]),
            ("cme_archive_daily", lambda: [(r[0], r[1]) for r in archive]),
            ("fci_value, total_head", lambda: [tuple(r) for r in printed]),
            ("cme_ftp_daily", lambda: [(r[0],) for r in printed]),
        ]

    def cursor(self):
        return FakeCursor(self._plans)


def _conn(archive, printed):
    return FakeConn(archive, printed)


def _select_lists(code):
    """Every 'SELECT ... FROM' column list appearing in `code`."""
    return re.findall(r"SELECT\s+(.*?)\s+FROM", code, re.S | re.I)


# ---------------------------------------------------------------------------
# The separation, which is the whole point.
# ---------------------------------------------------------------------------

def test_no_function_here_returns_a_blended_series():
    """
    THE GUARD THAT MATTERS. load_series returns the archive alone and
    agreement_by_year returns a comparison; neither hands back a sequence in
    which an as-printed value and a restated one are interchangeable. Asserted
    on the SOURCE, because the harm is a future edit, not today's behaviour:
    the only place fci_value may be read is the comparison, and it must never
    be written into the series load.
    """
    src = (APP_DIR / "archive_history.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fns = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}

    # The SELECT LIST, not the whole function. load_series legitimately says
    # WHERE fci_value IS NOT NULL -- a date CME printed with no index on it is
    # not a printed date -- and banning the identifier outright would forbid
    # that correct filter while still permitting the harmful thing, which is
    # FETCHING the value. What must never happen is the column arriving in a
    # row this function returns.
    for sel in _select_lists(ast.unparse(fns["load_series"])):
        assert "fci_value" not in sel, (
            "load_series fetches CME's published index into its own series; a "
            "caller can no longer tell which number it is holding: %s" % sel)

    # And the series rows carry no field that could be mistaken for one.
    a = [("2013-01-02", 150.0, 100)]
    rows = ah.load_series(_conn(a, []))
    assert rows == [("2013-01-02", 150.0, True)]
    assert len(rows[0]) == 3, "a fourth field would invite an fci_value here"


def test_that_the_blend_guard_can_fail():
    """
    Both halves of the rule, planted, or the test above is asserting nothing:
    a FETCH of the column must be caught, and the legitimate WHERE filter must
    NOT be -- a guard that rejects both would be deleted the first time it
    blocked correct code, taking the real protection with it.
    """
    fetches = "SELECT report_date, fci_value FROM cme_ftp_daily"
    assert any("fci_value" in sel for sel in _select_lists(fetches))

    filters = "SELECT report_date FROM cme_ftp_daily WHERE fci_value IS NOT NULL"
    assert not any("fci_value" in sel for sel in _select_lists(filters)), (
        "the guard rejects the correct filter too, so it will be deleted")


def test_the_module_draws_nothing():
    """Two dashboards with different palettes read this; a chart built here
    would be one repo's colours imposed on the other."""
    src = (APP_DIR / "archive_history.py").read_text(encoding="utf-8")
    for banned in ("import streamlit", "import plotly", "plotly.graph_objects"):
        assert banned not in src, banned


# ---------------------------------------------------------------------------
# archive_only, and the three different reasons a date can be missing.
# ---------------------------------------------------------------------------

SERIES = [
    # before the as-printed era -- exists in no other source
    ("2013-01-02", 150.00, 1000),
    ("2014-06-02", 160.00, 1000),
    # the as-printed era
    ("2015-01-02", 170.00, 1000),
    ("2015-06-02", 171.00, 1000),   # <- a HOLE: printed table lacks it
    ("2015-12-02", 172.00, 1000),
    # after the last as-printed date -- today's print, not fetched yet
    ("2016-01-04", 173.00, 1000),
]
PRINTED = [
    (date(2015, 1, 2), 170.00, 1000),
    (date(2015, 12, 2), 172.00, 1000),
]


def test_archive_only_is_derived_from_the_printed_table_not_a_2015_constant():
    rows = ah.load_series(_conn(SERIES, PRINTED))
    flagged = {d for d, _v, only in rows if only}
    assert flagged == {"2013-01-02", "2014-06-02", "2015-06-02", "2016-01-04"}


def test_a_hole_is_inside_the_printed_era_and_nothing_else():
    """
    Three different claims, and lumping them together is how the panel would
    be wrong. Before the era: no collection could have it. Inside: a gap in
    OURS, and worth acting on. After the last printed date: today's print,
    which is not a gap and must not be reported as one every afternoon.
    """
    rows = ah.load_series(_conn(SERIES, PRINTED))
    assert ah.printed_span(rows) == ("2015-01-02", "2015-12-02")
    assert ah.holes_in_printed(rows) == ["2015-06-02"]


def test_that_the_hole_rule_can_fail():
    """A rule of 'every archive-only date' must pick up the two kinds that are
    not holes -- otherwise the test above is asserting nothing."""
    rows = ah.load_series(_conn(SERIES, PRINTED))
    naive = [d for d, _v, only in rows if only]
    assert naive == ["2013-01-02", "2014-06-02", "2015-06-02", "2016-01-04"]
    assert ah.holes_in_printed(rows) == ["2015-06-02"]


def test_the_pre_era_count_excludes_holes_and_the_pending_print():
    s = ah.load_series(_conn(SERIES, PRINTED))
    m = ah.summary(s, ah.agreement_by_year(_conn(SERIES, PRINTED)))
    assert m["archive_only_dates"] == 2, (
        "the 'exists nowhere else' figure swept up a hole or a pending print")
    assert m["archive_only_last"] == "2014-06-02"
    assert m["printed_from"] == "2015-01-02" and m["printed_to"] == "2015-12-02"
    assert m["holes"] == ["2015-06-02"]


# ---------------------------------------------------------------------------
# The agreement table.
# ---------------------------------------------------------------------------

def test_agreement_buckets_by_index_date_not_by_source_workbook():
    """
    FC2018 carries 2019-04-01 and FC2022 carries 2023-04-03. Bucketing on
    source_year would file a 2019 date under 2018 -- misreporting the exact
    years this table exists to tell apart. The fake returns no source_year at
    all, so a reimplementation reaching for one cannot even run.
    """
    archive = [("2019-04-01", 142.00, 500)]
    printed = [(date(2019, 4, 1), 140.00, 500)]
    got = ah.agreement_by_year(_conn(archive, printed))
    assert [r["year"] for r in got] == [2019]
    assert got[0]["worst_gap"] == pytest.approx(2.00)


def test_agreement_skips_dates_the_printed_table_lacks():
    """A date with nothing to compare against contributes no gap. Counting it
    as agreement (gap 0) would flatter the archive precisely where it is
    unverifiable."""
    archive = [("2013-01-02", 150.00, 100), ("2015-01-02", 170.00, 100)]
    printed = [(date(2015, 1, 2), 170.00, 100)]
    got = ah.agreement_by_year(_conn(archive, printed))
    assert [r["year"] for r in got] == [2015]
    assert got[0]["shared_dates"] == 1


def test_head_mismatch_counts_only_a_real_difference():
    archive = [("2015-01-02", 170.0, 100), ("2015-01-05", 171.0, 100)]
    printed = [(date(2015, 1, 2), 170.0, 100), (date(2015, 1, 5), 171.0, 180)]
    got = ah.agreement_by_year(_conn(archive, printed))
    assert got[0]["head_mismatches"] == 1


def test_a_null_head_on_either_side_is_not_a_mismatch():
    archive = [("2015-01-02", 170.0, None)]
    printed = [(date(2015, 1, 2), 170.0, 100)]
    assert ah.agreement_by_year(_conn(archive, printed))[0]["head_mismatches"] == 0


# ---------------------------------------------------------------------------
# rounding_era_from -- derived, not the literal 2021.
# ---------------------------------------------------------------------------

def _agree(pairs):
    return [{"year": y, "shared_dates": 10, "worst_gap": g, "head_mismatches": 0}
            for y, g in pairs]


def test_the_rounding_era_is_the_start_of_the_UNBROKEN_tail():
    agreement = _agree([(2018, 2.34), (2019, 0.49), (2020, 0.16),
                        (2021, 0.0049), (2022, 0.0050)])
    assert ah.rounding_era_from(agreement) == 2021


def test_one_loose_year_inside_the_tail_moves_the_era_forward():
    """
    The claim is 'every year from here agrees', so a single bad year after the
    candidate start invalidates it. A rule that merely found the first quiet
    year would answer 2021 here and over-claim four years of precision.
    """
    agreement = _agree([(2021, 0.0049), (2022, 0.90), (2023, 0.0050),
                        (2024, 0.0049)])
    assert ah.rounding_era_from(agreement) == 2023
    first_quiet = next(a["year"] for a in agreement
                       if a["worst_gap"] <= ah.MATERIAL_GAP)
    assert first_quiet == 2021, "the weaker rule really does differ here"


def test_no_quiet_tail_at_all_returns_None():
    assert ah.rounding_era_from(_agree([(2020, 1.0), (2021, 2.0)])) is None


def test_the_threshold_clears_rounding_but_catches_2020():
    """0.0050 is pure 2dp rounding and must not flag; 0.1612 is 2020's real
    restatement and must. A threshold that cannot tell them apart makes the
    era meaningless in one direction or the other."""
    assert 0.0050 <= ah.MATERIAL_GAP
    assert 0.1612 > ah.MATERIAL_GAP


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------

def test_an_empty_archive_summarises_to_None_rather_than_raising():
    """The backfill is manual, so a machine that has never run it holds no
    rows. That is an empty panel, not an error."""
    assert ah.summary([], []) is None
    assert ah.load_series(_conn([], [])) == []


def test_summary_reports_both_eras_separately():
    series = ah.load_series(_conn(SERIES, PRINTED))
    agreement = _agree([(2018, 2.34), (2021, 0.0049), (2022, 0.0050)])
    m = ah.summary(series, agreement)
    assert m["rounding_era_from"] == 2021
    assert m["recent_worst"] == pytest.approx(0.0050)
    assert m["older_worst"] == pytest.approx(2.34)
    assert m["older_worst_year"] == 2018
    assert m["first"] == "2013-01-02" and m["last"] == "2016-01-04"


def test_the_snowflake_backend_date_type_is_handled():
    """db.iso() absorbs datetime.date vs ISO string. If the archive side were
    ever read without it, a Snowflake row would key as a date object and match
    no printed string -- every date would report as archive-only."""
    archive = [(date(2015, 1, 2), 170.0, 100)]
    printed = [(date(2015, 1, 2), 170.0, 100)]
    rows = ah.load_series(_conn(archive, printed))
    assert rows == [("2015-01-02", 170.0, False)], (
        "a date object leaked through; every date would read as archive-only")
