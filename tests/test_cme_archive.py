"""
CME's yearly workbooks: a RESTATED archive, kept apart from the scorecard.

THE WHOLE RISK IS ONE COLUMN BEING MISTAKEN FOR ANOTHER.
`cme_ftp_daily.fci_value` is the REPORTED INDEX CME printed on the day -- the
independent number this project's estimate is graded against, whose entire
value is that nobody here computed it. The workbooks publish a weighted
average CME recomputed later, and measured across every year both exist:

    2021-2026   worst gap 0.0049-0.0050   -- pure 2dp rounding
    2020        0.1612
    2019        0.4921   (80 head mismatches)
    2018        2.3374
    2017        0.4041
    2015-2016   0.5382 / 0.8710

Identical to as-printed in recent years, materially different in older ones.
So the archive earns its place -- 2013 and 2014 exist nowhere else, since
CME's as-printed year folders floor at 2015 -- and must never be read as the
published index. Nothing in these tables is named fci_value.

OFFLINE. The parser is exercised against hand-built sheets; nothing here
touches CME's FTP.
"""

import re
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cme_archive  # noqa: E402


class FakeSheet:
    """Enough of an xlrd sheet for _columns and the row walk."""

    def __init__(self, rows):
        self._rows = rows
        self.nrows = len(rows)
        self.ncols = max(len(r) for r in rows)

    def cell_value(self, r, c):
        row = self._rows[r]
        return row[c] if c < len(row) else ""


def test_excel_serials_land_on_the_right_day():
    """
    1899-12-30, not 12-31: Excel preserves a 1900 leap-year bug, and the
    wrong origin shifts every date by one while still looking plausible.

    Checked against the workbooks themselves: FC2015 opens on 2014-12-31 and
    FC2013 on 2012-12-31, each carrying the previous year's last trading day,
    and those are serials 42004 and 41274. An earlier draft of this test
    asserted 2015-01-02 and 2013-01-01 -- the dates a reader EXPECTS a year
    file to start on, which is exactly how an off-by-N epoch gets blessed.
    """
    assert cme_archive.excel_date(42004) == date(2014, 12, 31)
    assert cme_archive.excel_date(41274) == date(2012, 12, 31)
    assert cme_archive.excel_date("42004.0") == date(2014, 12, 31)
    # One day apart in, one day apart out -- the epoch cannot be off by a
    # constant without this drifting too.
    assert (cme_archive.excel_date(42005) - cme_archive.excel_date(42004)).days == 1


def test_a_non_numeric_or_empty_date_is_skipped_not_guessed():
    for junk in ("", None, "DATE", 0, -5):
        assert cme_archive.excel_date(junk) is None


def test_columns_keeps_the_FIRST_of_a_repeated_header():
    """
    THE BUG THIS EXISTS FOR. FC2014 carries a second block from column 34 --
    "Location", "Report Date", "Head Count" -- so LOCATION appears twice. A
    dict comprehension over enumerate() keeps the LAST, which pointed the
    parser at column 34 and made it read "Dunlap, IA" where a barn name
    belonged. It then matched no totals row and reported the year as empty:
    a clean, silent, plausible nothing.
    """
    sheet = FakeSheet([["DATE", "LOCATION", "state", "Location", "Report Date"]])
    col = cme_archive._columns(sheet)
    assert col["LOCATION"] == 1, "a later duplicate shadowed the real column"
    assert col["DATE"] == 0


def test_that_duplicate_header_guard_can_fail():
    """The old rule, planted, must pick the wrong column -- or the test above
    is asserting nothing."""
    hdr = ["DATE", "LOCATION", "state", "Location"]
    last_wins = {n.strip().upper(): i for i, n in enumerate(hdr) if n}
    assert last_wins["LOCATION"] == 3
    assert cme_archive._columns(FakeSheet([hdr]))["LOCATION"] == 1


def _sheet(rows):
    hdr = ["DATE", "LOCATION", "state", "TTL HEAD", "TTL WEIGHT",
           "WTD AVG WEIGHT", "WTD AVG PRICE"]
    return FakeSheet([hdr] + rows)


def _parse(sheet, monkeypatch):
    """Drive parse() against a fake sheet without xlrd or a workbook."""
    class FakeBook:
        def sheet_by_index(self, _i):
            return sheet

    fake = type(sys)("xlrd")
    fake.open_workbook = lambda **kw: FakeBook()
    monkeypatch.setitem(sys.modules, "xlrd", fake)
    return cme_archive.parse(b"ignored")


def test_totals_rows_become_aggregates_and_barns_become_locations(monkeypatch):
    serial = (date(2015, 1, 2) - cme_archive.EXCEL_EPOCH).days
    daily, locs = _parse(_sheet([
        [serial, "ST. JOSEPH", "MO", 136, 101780, 748.38, 237.5],
        [serial, "DAILY TOTALS", "///", 10601, 7963537, 751.2, 235.57],
        [serial, "SEVEN-DAY TOTALS", "///", 38547, 28696704, 744.46, 235.278],
    ]), monkeypatch)

    assert len(daily) == 1
    row = daily[0]
    assert row["report_date"] == date(2015, 1, 2)
    assert row["daily_head"] == 10601
    assert row["seven_day_head"] == 38547
    assert row["seven_day_wtd_price"] == pytest.approx(235.278)
    assert "fci_value" not in row, (
        "the archive must never carry the scorecard's column name")

    assert len(locs) == 1 and locs[0]["location"] == "ST. JOSEPH"
    assert locs[0]["state"] == "MO" and locs[0]["head"] == 136


def test_a_date_with_no_seven_day_row_is_dropped(monkeypatch):
    """A daily total without its seven-day partner is half a record, and the
    seven-day figure is the only one worth having."""
    serial = (date(2015, 1, 5) - cme_archive.EXCEL_EPOCH).days
    daily, _ = _parse(_sheet([
        [serial, "DAILY TOTALS", "///", 100, 70000, 700, 210.0],
    ]), monkeypatch)
    assert daily == []


def test_barns_with_no_qualifying_head_are_not_stored(monkeypatch):
    """Most rows on most days are zero -- a barn that sold nothing in the
    bracket. Storing them would quadruple the table to say nothing."""
    serial = (date(2015, 1, 2) - cme_archive.EXCEL_EPOCH).days
    _d, locs = _parse(_sheet([
        [serial, "ABERDEEN", "SD", 0, 0, 0, 0],
        [serial, "ADA", "OK", "", "", "", ""],
        [serial, "REAL BARN", "KS", 55, 40000, 727, 230.0],
        [serial, "SEVEN-DAY TOTALS", "///", 55, 40000, 727, 230.0],
    ]), monkeypatch)
    assert [l["location"] for l in locs] == ["REAL BARN"]


def test_a_workbook_without_the_columns_it_needs_raises(monkeypatch):
    """Rather than returning an empty year, which reads as 'CME published
    nothing' -- the failure mode the duplicate-header bug actually produced."""
    with pytest.raises(cme_archive.ArchiveError):
        _parse(FakeSheet([["SOMETHING", "ELSE"]]), monkeypatch)


def test_a_non_zip_is_refused_clearly():
    with pytest.raises(cme_archive.ArchiveError):
        cme_archive.inflate_only_member(b"not a zip at all")


def test_the_archive_tables_do_not_carry_the_scorecard_column_name():
    """
    The real risk surface is the SCHEMA, not the prose. If either archive
    table ever grows an `fci_value`, the restated average and CME's published
    index are one careless join away from being averaged together.

    An earlier draft grepped the module source with the docstrings stripped by
    splitting on triple quotes. That tripped on parse()'s own docstring, which
    names the column precisely in order to warn about it -- a guard that
    punishes the documentation for doing its job. Asserting on the DDL tests
    the thing that can actually cause harm.
    """
    import backfill_cme_archive as bf
    for ddl in (bf.DAILY_DDL, bf.LOC_DDL):
        assert "fci_value" not in ddl.lower(), ddl
    assert "cme_archive_daily" in bf.DAILY_DDL
    assert "cme_archive_locations" in bf.LOC_DDL
    # And the names must not collide with the as-printed tables.
    for ddl in (bf.DAILY_DDL, bf.LOC_DDL):
        assert "cme_ftp_daily" not in ddl
        assert "cme_ftp_locations" not in ddl


def _schema_sql():
    return (Path(__file__).resolve().parent.parent
            / "snowflake" / "01_schema.sql").read_text(encoding="utf-8")


def _migrate():
    """snowflake/02_migrate_data.py, loaded without running its __main__."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_archive_migrate",
        Path(__file__).resolve().parent.parent / "snowflake" / "02_migrate_data.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_snowflake_ddl_carries_the_same_guarantee_as_the_sqlite_one():
    """
    THE SECOND SCHEMA IS THE ONE PRODUCTION ACTUALLY READS, and the test above
    only ever looked at the first. backfill_cme_archive.py's DDL builds the
    local table; snowflake/01_schema.sql builds the one the dashboards query.
    A column named fci_value could appear in either and the harm is the same
    -- the restated average one careless join away from the published index --
    so both are asserted, and the one that matters more was the one missing.
    """
    sql = _schema_sql()
    for name in ("cme_archive_daily", "cme_archive_locations"):
        pat = r"CREATE TABLE IF NOT EXISTS %s \((.*?)\);" % name
        m = re.search(pat, sql, re.S)
        assert m, "no DDL for %s in 01_schema.sql" % name
        assert "fci_value" not in m.group(1).lower(), m.group(1)
    assert "seven_day_wtd_price" in sql, (
        "the archive's own column name is gone from the Snowflake schema")


def test_the_archive_is_pushed_as_OPTIONAL_and_never_as_CRITICAL():
    """
    CRITICAL means "if this fails the index is stale and somebody is paged
    tonight". These two tables are a manual backfill nothing on either
    dashboard reads, so listing them there would let a workbook parse put the
    index push into a failure state -- and, being last, would also let 43k
    rows of analysis data sit between a finished index and its stamp.
    """
    m = _migrate()
    for name in ("cme_archive_daily", "cme_archive_locations"):
        assert name in m.OPTIONAL_TABLES, name
        assert name not in m.CRITICAL_TABLES, name
    # Last, so a failure here delays no table anybody is looking at.
    assert m.OPTIONAL_TABLES[-2:] == ["cme_archive_daily", "cme_archive_locations"]
    # pipeline_stamp's position is the index's own guarantee and the archive
    # must not have displaced it off the end of the critical list.
    assert m.CRITICAL_TABLES[-1] == "pipeline_stamp"


def test_every_pushed_table_has_DDL_in_the_snowflake_schema():
    """
    A name in TABLES with no CREATE TABLE behind it is the failure
    02_migrate_data.py's own comment warns about: DESC TABLE runs outside the
    per-table try, so one absent name takes down every table after it. The
    schema file is where that gets caught before a 13:00 run finds it.
    """
    sql = _schema_sql()
    missing = [t for t in _migrate().TABLES
               if "CREATE TABLE IF NOT EXISTS %s (" % t not in sql]
    assert not missing, missing
