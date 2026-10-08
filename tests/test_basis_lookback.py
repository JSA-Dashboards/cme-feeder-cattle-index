"""
A barn's basis THEN, not an average of the months in between.

WHY THIS IS NOT JUST MORE WINDOWS. barn_basis already offers 180- and 365-day
windows, and they average basis across the whole period: a barn that ran +$12 all
spring and -$4 all autumn reads as +$4 over the year, a basis it never traded at
on any day. test_a_window_average_and_a_lookback_are_different_numbers builds
exactly that barn and pins the two apart.

WHAT THE LIVE DATA SAYS, measured 2026-10-07 on the 600-649 lb bracket, basis vs
FCI:

    Carthage MO     now +42.42   6 mo ago +66.81   12 mo ago +31.82
    West Plains MO  now +35.37   6 mo ago +75.32   12 mo ago +19.69
    Tulsa OK        now +29.65   6 mo ago +73.05   12 mo ago +21.90

Six months back is uniformly much wider and twelve months back is comparable to
now, across every barn -- which is the season moving, not the barns. That is why
the module treats the 12-month column as the like-for-like comparison and says so
in the caption rather than letting a reader take the 6-month swing for a barn's
own change.
"""
import sqlite3
import sys
import types

import pytest

if "streamlit" not in sys.modules:
    _stub = types.ModuleType("streamlit")
    _stub.cache_data = lambda **kw: (lambda f: f)
    sys.modules["streamlit"] = _stub

import barn_basis as bb


class FakeDb:
    def __init__(self, snowflake=False):
        self._sf = snowflake

    def use_snowflake(self):
        return self._sf

    @staticmethod
    def iso(v):
        return v


SCHEMA = """
CREATE TABLE calf_sales (
    report_date TEXT, slug_id INTEGER, location TEXT, state TEXT,
    weight_low INTEGER, weight_high INTEGER, muscle_grade TEXT,
    head_count INTEGER, avg_weight REAL, avg_price REAL);
CREATE TABLE fci_daily (
    report_date TEXT PRIMARY KEY, fci_value REAL, n_locations INTEGER,
    total_head INTEGER);
"""


@pytest.fixture
def db(monkeypatch, tmp_path):
    """
    FILE-BACKED, and _conn hands out a NEW connection every call -- which is what
    production does, and what a shared :memory: handle cannot survive. Both
    load_barns and load_basis_lookbacks close the connection in their finally
    block, so the first of them to run would close it under the second, and the
    second would return its quiet "backend will not open" result. The test that
    compares a window against a lookback calls both, and failed exactly there.
    """
    monkeypatch.delenv("USE_SNOWFLAKE", raising=False)
    path = tmp_path / "t.db"
    setup = sqlite3.connect(path)
    setup.executescript(SCHEMA)
    setup.commit()
    setup.close()
    monkeypatch.setattr(bb, "_conn", lambda: (FakeDb(), sqlite3.connect(path)))

    class Writer:
        """Opens per write so it never holds a handle the readers can close."""

        def execute(self, sql, args=()):
            c = sqlite3.connect(path)
            try:
                c.execute(sql, args)
                c.commit()
            finally:
                c.close()

        def commit(self):
            pass

    return Writer()


def sale(conn, days_ago, barn, price, index, head=100, state="MO", weight_low=600):
    """One sale and the index on that same day -- load_barns inner-joins them."""
    conn.execute(
        "INSERT INTO calf_sales (report_date, slug_id, location, state, "
        "weight_low, weight_high, muscle_grade, head_count, avg_weight, avg_price) "
        "VALUES (date('now','-' || ? || ' day'),1,?,?,?,?, '1',?,625.0,?)",
        (int(days_ago), barn, state, weight_low, weight_low + 50, head, price))
    conn.execute(
        "INSERT OR REPLACE INTO fci_daily (report_date, fci_value, n_locations, "
        "total_head) VALUES (date('now','-' || ? || ' day'),?,10,1000)",
        (int(days_ago), index))
    conn.commit()


# ---------------------------------------------------------------------------
# The band.
# ---------------------------------------------------------------------------

def test_the_band_is_centred_and_qualifies_the_joined_table():
    """
    `cs.` prefix is load-bearing: the query joins calf_sales to fci_daily and
    both carry report_date, so an unqualified column is ambiguous.
    """
    sql = bb._band(FakeDb(), 182, width=30)
    assert "cs.report_date BETWEEN" in sql
    assert "-197 day" in sql and "-167 day" in sql, sql


def test_the_band_works_on_snowflake_too():
    sf = bb._band(FakeDb(True), 365)
    assert "DATEADD(day, -380, CURRENT_DATE())" in sf
    assert "date(" not in sf


# ---------------------------------------------------------------------------
# The measure.
# ---------------------------------------------------------------------------

def test_basis_is_price_minus_the_index_on_the_barns_own_sale_day(db):
    sale(db, 182, "Carthage", 440.0, 373.19)
    got = bb.load_basis_lookbacks(600)[("Carthage", "MO")][182]
    assert got["basis"] == pytest.approx(66.81, abs=0.01)


def test_the_lookback_is_HEAD_weighted_like_load_barns(db):
    """
    Not pound-weighted. cash_calves.py is pound-weighted for its own reasons;
    copying that here would make a barn's history disagree with the $/cwt beside
    it by a few cents for no visible reason.
    """
    sale(db, 182, "Mixed", 400.0, 360.0, head=300)
    sale(db, 182, "Mixed", 600.0, 360.0, head=10)
    got = bb.load_basis_lookbacks(600)[("Mixed", "MO")][182]["basis"]
    want = (300 * 400 + 10 * 600) / 310 - 360.0
    assert got == pytest.approx(want, abs=0.01)
    assert got != pytest.approx(500.0 - 360.0, abs=1.0), "a plain mean would give 140"


def test_rel_is_measured_against_the_bracket_AS_IT_WAS_THEN(db):
    """
    Not against today's bracket. A barn compared to the current market would
    read as having moved when the whole market did -- which, on the live data,
    is exactly what the 6-month column would have shown for every barn at once.
    """
    sale(db, 182, "Wide", 460.0, 400.0, head=100)     # basis +60
    sale(db, 182, "Narrow", 420.0, 400.0, head=100)   # basis +20
    got = bb.load_basis_lookbacks(600)
    # bracket basis then = head-weighted mean of +60 and +20 = +40
    assert got[("Wide", "MO")][182]["rel"] == pytest.approx(20.0, abs=0.01)
    assert got[("Narrow", "MO")][182]["rel"] == pytest.approx(-20.0, abs=0.01)


def test_a_window_average_and_a_lookback_are_DIFFERENT_numbers(db):
    """
    THE REASON THIS IS NOT TWO MORE WINDOWS ENTRIES. One barn, +60 basis in the
    spring band and +10 in the autumn band. A 365-day WINDOW averages them; the
    lookback columns report each where it happened.
    """
    for d in (180, 182, 184):
        sale(db, d, "Swing", 460.0, 400.0, head=100)      # +60
    for d in (363, 365, 367):
        sale(db, d, "Swing", 410.0, 400.0, head=100)      # +10
    back = bb.load_basis_lookbacks(600)[("Swing", "MO")]
    assert back[182]["basis"] == pytest.approx(60.0, abs=0.01)
    assert back[365]["basis"] == pytest.approx(10.0, abs=0.01)

    rows, _bracket = bb.load_barns(600, 365)
    windowed = next(r for r in rows if r["barn"] == "Swing")["basis"]
    # 5 of the 6 sales fall inside a 365-day window (the one at 367 days does
    # not), so the blend is (3 x +60 + 2 x +10) / 5 = +40.
    assert windowed == pytest.approx(40.0, abs=0.01), (
        "the window average blends the two into a basis the barn never traded")
    assert windowed not in (back[182]["basis"], back[365]["basis"])


# ---------------------------------------------------------------------------
# Absence.
# ---------------------------------------------------------------------------

def test_a_barn_absent_from_a_band_is_absent_not_zero(db):
    """
    Zero basis is a real and unremarkable reading on this tab -- a barn trading
    exactly at the index. So a filled-in 0.00 is indistinguishable from data.
    """
    sale(db, 365, "Seasonal", 420.0, 400.0)
    got = bb.load_basis_lookbacks(600)[("Seasonal", "MO")]
    assert set(got) == {365}
    assert 182 not in got


def test_a_sale_outside_the_band_does_not_count(db):
    sale(db, 220, "Edge", 460.0, 400.0)
    assert ("Edge", "MO") not in bb.load_basis_lookbacks(600)


def test_a_sale_inside_the_band_does_count(db):
    """The other half -- a zero-width band would satisfy the test above."""
    sale(db, 190, "Edge", 460.0, 400.0)
    assert bb.load_basis_lookbacks(600)[("Edge", "MO")][182]["basis"] == \
        pytest.approx(60.0, abs=0.01)


def test_a_sale_with_no_index_that_day_is_dropped_not_zeroed(db):
    """
    The inner join load_barns documents at length: fci_daily covers every
    calendar day, and a sale with no index row is a push-ordering artifact. A
    null index must not become a basis equal to the full price.
    """
    db.execute(
        "INSERT INTO calf_sales (report_date, slug_id, location, state, "
        "weight_low, weight_high, muscle_grade, head_count, avg_weight, avg_price) "
        "VALUES (date('now','-182 day'),1,'Orphan','MO',600,650,'1',100,625.0,460.0)")
    db.commit()
    assert ("Orphan", "MO") not in bb.load_basis_lookbacks(600)


def test_a_MIX_of_joined_and_unjoined_sales_does_not_dilute_the_index(db):
    """
    The case that actually distinguishes the inner join, and the one the test
    above does NOT reach.

    A barn whose sales ALL lack an index row is dropped either way -- the null
    fci fails the `if d and e` guard. But a barn with SOME sales unjoined is
    different: under a LEFT JOIN, SUM(fci*head) skips the nulls while
    SUM(head) counts them, so the index comes out understated and the basis
    correspondingly too wide. Here that is +60 read as +130.

    Found by mutation: swapping JOIN for LEFT JOIN left the orphan test green.
    """
    sale(db, 182, "Partial", 460.0, 400.0, head=100)     # joined, basis +60
    db.execute(                                          # same barn, no index row
        "INSERT INTO calf_sales (report_date, slug_id, location, state, "
        "weight_low, weight_high, muscle_grade, head_count, avg_weight, avg_price) "
        "VALUES (date('now','-183 day'),1,'Partial','MO',600,650,'1',100,625.0,460.0)")
    got = bb.load_basis_lookbacks(600)[("Partial", "MO")][182]["basis"]
    assert got == pytest.approx(60.0, abs=0.01), (
        "only the sale paired with its own index day may count")
    assert got < 100, "a diluted index would read this barn at roughly +130"


def test_the_weight_bracket_is_honoured(db):
    sale(db, 182, "Carthage", 460.0, 400.0, weight_low=600)
    sale(db, 182, "Carthage", 999.0, 400.0, weight_low=650)
    assert bb.load_basis_lookbacks(600)[("Carthage", "MO")][182]["basis"] == \
        pytest.approx(60.0, abs=0.01)


def test_a_missing_table_leaves_the_columns_empty(monkeypatch):
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(bb, "_conn", lambda: (FakeDb(), conn))
    assert bb.load_basis_lookbacks(600) == {}


def test_both_modes_are_available_for_every_entry(db):
    """render picks one by the toggle, so both must always be present."""
    sale(db, 182, "Carthage", 460.0, 400.0)
    entry = bb.load_basis_lookbacks(600)[("Carthage", "MO")][182]
    assert set(entry) == {"basis", "rel"}


def test_the_offsets_are_roughly_six_and_twelve_months():
    centres = [c for c, _ in bb.LOOKBACKS]
    assert centres == sorted(centres)
    assert 170 <= centres[0] <= 195 and 355 <= centres[1] <= 380, centres
