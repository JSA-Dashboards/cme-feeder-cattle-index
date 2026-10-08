"""
"What were 550s worth six months ago" is a different question from "average the
last six months", and this is the test that keeps them apart.

WHY IT IS A COLUMN AND NOT A LONGER WINDOW. The Window control averages over its
whole period, so adding 180 and 365 to WINDOWS would produce one number blended
across a year of a trending market -- a price that was never true on any day.
Measured 2026-10-07 on the 550-600 lb bracket: monthly averages ran $396.75 in
Aug 2026 to $479.99 in Apr 2026, an $83 swing, and the 12-month average is
$446.61 against a 30-day reading of $411.47. That is $35 above the live market,
in a tile captioned "what cattle are worth" -- the hay-units trap in CLAUDE.md
wearing a different unit, a figure arithmetically correct and answering a
question nobody asked.

So the lookback is a pound-weighted average over a 30-day band CENTRED on the
date, computed the same way as the live reading so the two are comparable.
"""
import sqlite3
import sys
import types

import pytest

# cash_calves imports streamlit at module scope; stub it before the import so the
# module can be exercised headless. cache_data must pass the function through
# unchanged or every call below would be memoised across tests.
if "streamlit" not in sys.modules:
    _stub = types.ModuleType("streamlit")
    _stub.cache_data = lambda **kw: (lambda f: f)
    sys.modules["streamlit"] = _stub

import cash_calves as cc


class FakeDb:
    """snowflake_db's surface, for the two branches _since() takes."""

    def __init__(self, snowflake=False):
        self._sf = snowflake

    def use_snowflake(self):
        return self._sf

    @staticmethod
    def iso(v):
        return v


# ---------------------------------------------------------------------------
# The band: centred, not trailing.
# ---------------------------------------------------------------------------

def test_the_band_is_CENTRED_on_the_date_not_trailing():
    """
    A trailing 30 days ending 182 back is really 6 to 7 months ago, and would
    make every lookback silently older than its own label.
    """
    sql = cc._band(FakeDb(), 182, width=30)
    assert "-197 day" in sql and "-167 day" in sql, sql
    assert "BETWEEN" in sql


def test_the_band_works_on_both_backends():
    """Dual backend: SQLite takes date(), Snowflake takes DATEADD."""
    assert "date('now','-197 day')" in cc._band(FakeDb(False), 182)
    sf = cc._band(FakeDb(True), 182)
    assert "DATEADD(day, -197, CURRENT_DATE())" in sf
    assert "date(" not in sf


def test_the_band_is_derived_from_the_width_not_hardcoded():
    """A literal 15 either side would ignore LOOKBACK_BAND_DAYS."""
    wide = cc._band(FakeDb(), 365, width=60)
    assert "-395 day" in wide and "-335 day" in wide, wide


# ---------------------------------------------------------------------------
# The data path.
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE calf_sales (
    report_date TEXT, raw_date TEXT, published_date TEXT, slug_id INTEGER,
    location TEXT, state TEXT, weight_low INTEGER, weight_high INTEGER,
    muscle_grade TEXT, head_count INTEGER, avg_weight REAL, avg_price REAL)
"""


@pytest.fixture
def db(monkeypatch):
    monkeypatch.delenv("USE_SNOWFLAKE", raising=False)
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    monkeypatch.setattr(cc, "_conn", lambda: (FakeDb(), conn))
    return conn


def sale(conn, days_ago, barn, price, head=100, weight=575.0, state="MO",
         weight_low=550):
    conn.execute(
        "INSERT INTO calf_sales (report_date, slug_id, location, state, "
        "weight_low, weight_high, muscle_grade, head_count, avg_weight, avg_price) "
        "VALUES (date('now','-' || ? || ' day'),1,?,?,?,?, '1',?,?,?)",
        (int(days_ago), barn, state, weight_low, weight_low + 50, head,
         weight, price))
    conn.commit()


def test_a_barn_gets_its_own_price_at_each_offset(db):
    sale(db, 182, "Joplin", 480.0)
    sale(db, 365, "Joplin", 443.0)
    got = cc.load_lookbacks(550, cc.ALL_STATES)
    assert got[("Joplin", "MO")] == {182: 480.0, 365: 443.0}


def test_a_barn_with_no_prints_in_a_band_is_ABSENT_not_zero(db):
    """
    Barns open, close and move sale days. Philip SD had no 550s six months ago
    in the live data; the honest answer is nothing, and the table renders it
    blank. A zero would sort to the bottom and read as a price collapse.
    """
    sale(db, 365, "Philip", 491.0, state="SD")
    got = cc.load_lookbacks(550, cc.ALL_STATES)
    assert got[("Philip", "SD")] == {365: 491.0}
    assert 182 not in got[("Philip", "SD")]


def test_a_band_that_returns_a_row_with_NO_USABLE_PRICE_is_absent_too(db):
    """
    The case the test above does NOT reach, and the one the `if price:` guard is
    actually for. A barn with no prints in the band returns no row at all, so
    the dict stays clean whatever the guard does. But a barn WITH rows in the
    band whose weighted head is zero returns a row with a NULL price -- the
    NULLIF division -- and without the guard that lands in the table as $0.00.

    Found by mutation: replacing the guard with `float(price or 0.0)` left the
    test above green, which made it decoration for this rule.
    """
    sale(db, 182, "Ghost", 480.0, head=0)         # in band, nothing behind it
    sale(db, 365, "Ghost", 455.0, head=100)
    got = cc.load_lookbacks(550, cc.ALL_STATES).get(("Ghost", "MO"), {})
    assert got.get(365) == 455.0, "the real reading must still come through"
    assert 182 not in got, "a NULL price must not become $0.00"


def test_a_sale_just_OUTSIDE_the_band_does_not_count(db):
    """±15 days. 200 days back is outside a band centred on 182."""
    sale(db, 200, "Edge", 400.0)
    assert ("Edge", "MO") not in cc.load_lookbacks(550, cc.ALL_STATES)


def test_a_sale_just_INSIDE_the_band_does_count(db):
    """The other half -- without it, a band of zero width passes the test above."""
    sale(db, 190, "Edge", 400.0)
    assert cc.load_lookbacks(550, cc.ALL_STATES)[("Edge", "MO")][182] == 400.0


def test_the_lookback_is_POUND_weighted_like_the_live_reading(db):
    """
    Same formula as load_rows, or the columns are not comparable. A 300-head
    draft at $400 and a 10-head pen at $600 is $406.45, not the $500 a plain
    mean would give.
    """
    sale(db, 182, "Mixed", 400.0, head=300, weight=575.0)
    sale(db, 182, "Mixed", 600.0, head=10, weight=575.0)
    got = cc.load_lookbacks(550, cc.ALL_STATES)[("Mixed", "MO")][182]
    assert got == pytest.approx((300 * 400 + 10 * 600) / 310, abs=0.01)
    assert got != pytest.approx(500.0, abs=1.0), "a plain mean would give 500"


def test_the_weight_bracket_is_honoured(db):
    sale(db, 182, "Joplin", 480.0, weight_low=550)
    sale(db, 182, "Joplin", 999.0, weight_low=600)
    assert cc.load_lookbacks(550, cc.ALL_STATES)[("Joplin", "MO")][182] == 480.0


def test_the_state_filter_is_honoured(db):
    sale(db, 182, "Joplin", 480.0, state="MO")
    sale(db, 182, "Beaver", 460.0, state="OK")
    got = cc.load_lookbacks(550, "OK")
    assert list(got) == [("Beaver", "OK")]


def test_a_missing_table_leaves_the_columns_empty_rather_than_breaking(monkeypatch):
    """A cash feed that is down should leave a quiet page, same as load_rows."""
    conn = sqlite3.connect(":memory:")          # no calf_sales at all
    monkeypatch.setattr(cc, "_conn", lambda: (FakeDb(), conn))
    assert cc.load_lookbacks(550, cc.ALL_STATES) == {}


# ---------------------------------------------------------------------------
# The decision itself, guarded.
# ---------------------------------------------------------------------------

def test_the_long_lookbacks_are_NOT_in_the_window_list():
    """
    The thing this feature deliberately did not do. Someone "simplifying" the
    lookback into two more WINDOWS entries would reintroduce the blended
    year-average in a tile that says it is the current price.
    """
    assert 180 not in cc.WINDOWS and 182 not in cc.WINDOWS
    assert 365 not in cc.WINDOWS
    assert cc.WINDOWS == [14, 30, 60, 90]


def test_the_offsets_are_roughly_six_and_twelve_months():
    centres = [c for c, _ in cc.LOOKBACKS]
    assert centres == sorted(centres), "render reads LOOKBACKS in order"
    assert 170 <= centres[0] <= 195, centres
    assert 355 <= centres[1] <= 380, centres
