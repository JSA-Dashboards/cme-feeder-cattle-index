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
import datetime as _dt
import pathlib
import sqlite3
import sys
import types

import pytest

if "streamlit" not in sys.modules:
    _stub = types.ModuleType("streamlit")
    _stub.cache_data = lambda **kw: (lambda f: f)
    # column_config, because blank_missing_lookbacks asks for a TextColumn to
    # right-align what it has just turned into text. The stub records the
    # kwargs rather than ignoring them, so a test can see WHICH columns were
    # configured without needing real streamlit.
    _stub.column_config = types.SimpleNamespace(
        TextColumn=lambda **kw: ("TextColumn", kw))
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
    # These tests are about the MEASURE -- head-weighting, the inner join, the
    # band's edges, rel against the bracket as it was. None is about whether
    # fci_daily can be trusted that far back, so the cutoff is moved out of the
    # way and the tests that ARE about it put it back explicitly. Leaving the
    # real date here would make every one of them assert the blanking rule by
    # accident and stop testing what its name says.
    monkeypatch.setattr(bb, "INDEX_TRUSTWORTHY_FROM", _dt.date(2000, 1, 1))
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


# ---------------------------------------------------------------------------
# The index is not trustworthy all the way back, and the columns say so.
# ---------------------------------------------------------------------------

def test_a_band_reaching_before_the_cutoff_omits_the_vs_FCI_basis(db, monkeypatch):
    """
    THE 2026-10-21 FINDING. fci_daily did not carry direct-trade sales until
    2026-08-28. Measured against CME's own file, it ran $2.814 out on average
    over the six-month band and $1.691 over the twelve-month one, against
    $0.005 today. "vs FCI 6 mo ago" was quoting a basis to the cent off an
    index that was dollars wrong, in the same row as a column accurate to half
    a cent, under a caption saying the two were comparable.
    """
    monkeypatch.setattr(bb, "INDEX_TRUSTWORTHY_FROM", _dt.date(2026, 8, 28))
    sale(db, 182, "Carthage", 460.0, 400.0)
    got = bb.load_basis_lookbacks(600)[("Carthage", "MO")][182]
    assert "basis" not in got, "vs FCI must be absent for an untrustworthy band"
    assert "rel" in got, "vs bracket survives -- the index error cancels"


def test_vs_bracket_survives_because_the_index_error_cancels(db, monkeypatch):
    """
    The asymmetry is deliberate, not an oversight. rel is a barn's basis minus
    the whole bracket's, both built from the same fci_daily on the same sale
    days, so a wrong index very largely drops out. Mode A has nothing to stand
    on; mode B compares barns with each other.
    """
    monkeypatch.setattr(bb, "INDEX_TRUSTWORTHY_FROM", _dt.date(2026, 8, 28))
    sale(db, 182, "Wide", 460.0, 400.0, head=100)
    sale(db, 182, "Narrow", 420.0, 400.0, head=100)
    got = bb.load_basis_lookbacks(600)
    assert got[("Wide", "MO")][182]["rel"] == pytest.approx(20.0, abs=0.01)
    assert got[("Narrow", "MO")][182]["rel"] == pytest.approx(-20.0, abs=0.01)


def test_the_cutoff_needs_the_WHOLE_band_not_just_its_newest_day(db, monkeypatch):
    """
    A straddling band is part good and part dollars out, and nothing says which
    side a given barn's sale fell on. Newest-day-only would have let the
    six-month band through from about 2027-02-25, quoting a number that is
    half wrong.
    """
    import datetime as dt
    monkeypatch.setattr(bb, "INDEX_TRUSTWORTHY_FROM", dt.date(2026, 8, 28))
    cutoff = bb.INDEX_TRUSTWORTHY_FROM
    half = bb.LOOKBACK_BAND_DAYS // 2
    # a 'today' where the band's newest day clears the cutoff but its oldest does not
    straddle = cutoff + dt.timedelta(days=182 - half + 1)
    oldest, newest = bb.band_dates(182, today=straddle)
    assert oldest < cutoff <= newest, (oldest, cutoff, newest)
    assert not bb.index_is_trustworthy(182, today=straddle)


def test_the_cutoff_opens_up_once_the_band_clears_it(db, monkeypatch):
    """The other half -- a rule that never admits anything is not a rule."""
    import datetime as dt
    monkeypatch.setattr(bb, "INDEX_TRUSTWORTHY_FROM", dt.date(2026, 8, 28))
    clear = bb.INDEX_TRUSTWORTHY_FROM + dt.timedelta(days=182 + 20)
    assert bb.index_is_trustworthy(182, today=clear)
    assert bb.band_dates(182, today=clear)[0] >= bb.INDEX_TRUSTWORTHY_FROM


def test_band_dates_mirrors_the_sql_band():
    """
    band_dates answers questions about the window _band builds. If they drift,
    the cutoff is applied to a different span than the query reads.
    """
    import datetime as dt
    today = dt.date(2026, 10, 21)
    oldest, newest = bb.band_dates(182, today=today)
    sql = bb._band(FakeDb(), 182)
    assert "-%d day" % (today - oldest).days in sql
    assert "-%d day" % (today - newest).days in sql


def test_the_cutoff_date_renders_on_windows():
    """
    %-d is glibc-only and raises ValueError on Windows, which is where this job
    runs. _fmt_date in this module already carries that warning and the caption
    fell into it anyway -- the first version used {date:%-d %B %Y} and would
    have thrown on every page load.
    """
    text = bb._cutoff_text()
    assert text == "28 August 2026", text
    assert not text.startswith("0")


# ---------------------------------------------------------------------------
# Rendering. Both of these shipped and were only visible on the page.
# ---------------------------------------------------------------------------

def test_an_all_absent_lookback_column_is_DROPPED_not_rendered():
    """
    NEITHER None NOR NaN RENDERS AS BLANK, and both shipped before this test
    existed. Measured on this pandas/pyarrow:

        column of None        dtype=object   arrow -> ['None', 'None']
        column of float64 NaN dtype=float64  arrow -> ['None', 'None']

    pyarrow serialises NaN to null and st.dataframe prints the word, so an
    all-absent column reaches the page as a stripe of "None" either way. With
    the index cutoff in force that is every row of both lookback columns on a
    traded table.

    The fix is to drop the column.

    AN EARLIER VERSION OF THIS TEST ALSO ASSERTED pyarrow's to_pylist(), on the
    theory that it showed NaN "arriving as None". It does not show that: nulls
    print as None in Python whatever produced them, so the assertion held for a
    blank, a NaN and a None alike and could not have failed. It is gone, and
    what a gap actually renders as is pinned by
    test_the_blank_check_REJECTS_the_two_versions_that_shipped below.
    """
    import pandas as pd

    short = "vs FCI"
    label = bb.LOOKBACKS[0][1]
    empty = pd.DataFrame([{"Sale barn": "A", "%s %s" % (short, label): float("nan")},
                          {"Sale barn": "B", "%s %s" % (short, label): float("nan")}])
    assert "%s %s" % (short, label) not in bb.drop_empty_lookbacks(empty, short).columns


def test_a_SPARSE_lookback_column_is_kept():
    """
    The other half. One barn missing a band is a fact about that barn, and the
    column still carries real numbers to sort on -- dropping it would hide them.
    A rule that drops every column with any gap would empty the table on the
    first barn that skipped a week.
    """
    import pandas as pd
    short, label = "vs FCI", bb.LOOKBACKS[0][1]
    col = "%s %s" % (short, label)
    sparse = pd.DataFrame([{"Sale barn": "A", col: 12.34},
                           {"Sale barn": "B", col: float("nan")}])
    kept = bb.drop_empty_lookbacks(sparse, short)
    assert col in kept.columns
    assert kept[col].iloc[0] == 12.34


def test_the_blanking_caption_escapes_its_dollar_signs():
    """
    _md's own docstring: Streamlit markdown reads a PAIR of dollar signs as
    LaTeX and eats everything between, raising nothing. The blanking caption
    names two prices -- $2.81 and $1.69 -- and shipped unescaped, so the page
    rendered "the index ran 2.81 out on average over the six-month band and
    1.69 ..." with the middle set in maths.
    """
    src = (pathlib.Path(bb.__file__)).read_text(encoding="utf-8")
    block = src.split("are blank in this mode on purpose")[1].split("st.caption")[0]
    assert "$2.81" in block and "$1.69" in block, "re-point this at the wording"
    head = src.split("are blank in this mode on purpose")[0]
    assert head.rstrip().endswith("st.caption(_md(") or "_md(" in head[-80:], \
        "the caption naming two prices must go through _md()"


# ---------------------------------------------------------------------------
# A missing cell, not a missing column. drop_empty_lookbacks handles the column
# that says nothing at all; these cover the gap INSIDE a column that is kept.
# ---------------------------------------------------------------------------

def _rendered(series):
    """
    What st.dataframe would put in each cell.

    A NULL IS NOT A BLANK -- measured in a browser against Streamlit 1.63 on a
    column of real numbers with one cell missing: float NaN, pandas pd.NA,
    NumberColumn(format=...) and a Styler with na_rep="" ALL print the word
    "None"; only a string column holding "" renders empty. So anything still
    null by the time it reaches the page is a cell that reads "None".
    """
    import pandas as pd
    return ["None" if pd.isna(v) else v for v in series]


def test_a_missing_lookback_cell_renders_BLANK_not_the_word_None():
    import pandas as pd
    short, label = "vs bracket", bb.LOOKBACKS[0][1]
    col = "%s %s" % (short, label)
    sparse = pd.DataFrame([{"Sale barn": "Valentine", col: 35.3},
                           {"Sale barn": "Philip", col: float("nan")}])
    out, _cfg = bb.blank_missing_lookbacks(sparse, short)
    assert _rendered(out[col]) == ["35.30", ""]


def test_the_blank_check_REJECTS_the_two_versions_that_shipped():
    """
    THE GUARD PROVEN TO FAIL. Both earlier fixes -- a column of None, then a
    column of float NaN -- passed the whole suite and rendered "None" on the
    traded page. If _rendered above cannot tell them from a blank then neither
    can the test above, and this one would have been just as green as the suite
    was on the day both shipped.
    """
    import pandas as pd
    assert _rendered(pd.Series([35.3, float("nan")])) == [35.3, "None"]
    assert _rendered(pd.Series([35.3, None])) == [35.3, "None"]
    assert _rendered(pd.Series(["35.30", ""])) == ["35.30", ""]


def test_a_present_lookback_value_keeps_both_decimals():
    """
    "35.3" beside "33.16" in a column of dollars reads as a different precision,
    not a rounder number. The float column got this free from the grid; a text
    column has to do it.
    """
    import pandas as pd
    short, label = "vs bracket", bb.LOOKBACKS[0][1]
    col = "%s %s" % (short, label)
    out, _cfg = bb.blank_missing_lookbacks(
        pd.DataFrame([{col: 35.3}, {col: -18.5}, {col: 0.0}]), short)
    assert list(out[col]) == ["35.30", "-18.50", "0.00"]


def test_zero_is_a_reading_and_survives_the_blanking():
    """
    Zero basis is real and ordinary here -- a barn that traded at the bracket.
    Blanking it would be the same error as filling a gap with zero, in reverse.
    """
    import pandas as pd
    short, label = "vs bracket", bb.LOOKBACKS[0][1]
    col = "%s %s" % (short, label)
    out, _cfg = bb.blank_missing_lookbacks(pd.DataFrame([{col: 0.0}]), short)
    assert list(out[col]) == ["0.00"]


def test_the_blanked_columns_are_right_aligned_like_their_neighbours():
    import pandas as pd
    short = "vs bracket"
    cols = {"%s %s" % (short, lbl): 1.0 for _c, lbl in bb.LOOKBACKS}
    _out, cfg = bb.blank_missing_lookbacks(pd.DataFrame([cols]), short)
    assert set(cfg) == set(cols)


def test_blanking_skips_a_column_drop_empty_lookbacks_already_removed():
    """
    The two run in order on the same frame, and in vs FCI mode the cutoff means
    drop_empty_lookbacks takes BOTH columns out first. A KeyError here would
    take down the tab in exactly the mode the blanking was built for.
    """
    import pandas as pd
    out, cfg = bb.blank_missing_lookbacks(
        pd.DataFrame([{"Sale barn": "A"}]), "vs FCI")
    assert cfg == {} and list(out.columns) == ["Sale barn"]


def test_the_two_tabs_agree_on_what_a_gap_looks_like():
    """
    Cash Feeder Prices sits one tab over and builds the same lookback columns.
    Two tabs disagreeing about whether a gap is blank or the word "None" is the
    kind of difference a reader reads as data.
    """
    import pandas as pd
    import cash_calves as cc
    label = cc.LOOKBACKS[0][1]
    out, cfg = cc.blank_missing_lookbacks(
        pd.DataFrame([{label: 412.5}, {label: float("nan")}]))
    assert _rendered(out[label]) == ["412.50", ""]
    assert set(cfg) == {label}


# ---------------------------------------------------------------------------
# One barn against its bracket, over time. load_barn_history().
# ---------------------------------------------------------------------------

@pytest.fixture
def hist_db(tmp_path, monkeypatch):
    """
    calf_sales only -- load_barn_history never touches fci_daily, which is the
    point of it. File-backed, because the loader closes the connection it is
    handed and a shared :memory: would vanish under the second call.
    """
    path = tmp_path / "hist.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE calf_sales (report_date TEXT, location TEXT, state TEXT,"
        " weight_low INTEGER, head_count INTEGER, avg_weight REAL,"
        " avg_price REAL)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(
        bb, "_conn", lambda: (FakeDb(), sqlite3.connect(path)))
    return path


def _sold(path, iso, barn, head, price, state="MO", weight_low=700):
    c = sqlite3.connect(path)
    c.execute("INSERT INTO calf_sales VALUES (?,?,?,?,?,?,?)",
              (iso, barn, state, weight_low, head, 725.0, price))
    c.commit()
    c.close()


def test_the_bracket_line_is_every_barn_not_just_this_one(hist_db):
    """
    The comparison is the barn against its PEERS. If the bracket were computed
    from the selected barn alone the premium would be zero on every day, and
    the lower panel would be a flat line nobody could read anything from.
    """
    _sold(hist_db, "2026-10-01", "Joplin", 100, 400.0)
    _sold(hist_db, "2026-10-01", "Carthage", 100, 300.0)
    got = bb.load_barn_history("Joplin", "MO", 700)
    assert len(got) == 1
    assert got[0]["price"] == 400.0
    assert got[0]["bracket"] == 350.0, (
        "the bracket is not being taken over every barn: %r" % got[0])


def test_a_ONE_HEAD_PRINT_is_not_a_point_on_the_line(hist_db):
    """
    THE GUARD. The five most extreme premiums in the live data came from
    barn-days of 1, 7, 5, 7 and 1 head -- one of them -120.75 against a bracket
    the barn normally tracks within a few dollars. They were the extremes of
    the chart.

    MIN_PRINT_HEAD is deliberately NOT MIN_HEAD: that one gates a barn over a
    whole window, this gates one barn on one day, and at 700-749 lb the median
    barn-day is 41 head.
    """
    _sold(hist_db, "2026-10-01", "Joplin", 100, 400.0)
    _sold(hist_db, "2026-10-01", "Carthage", 100, 400.0)
    _sold(hist_db, "2026-10-02", "Joplin", 1, 250.0)
    _sold(hist_db, "2026-10-02", "Carthage", 100, 400.0)
    got = bb.load_barn_history("Joplin", "MO", 700)
    assert [r["date"] for r in got] == ["2026-10-01"], (
        "a one-head print reached the line: %r" % [r["date"] for r in got])


def test_a_day_on_the_threshold_is_kept(hist_db):
    """The control: a floor that also dropped ordinary days would thin the
    series rather than clean it."""
    _sold(hist_db, "2026-10-01", "Joplin", bb.MIN_PRINT_HEAD, 400.0)
    _sold(hist_db, "2026-10-01", "Carthage", 100, 400.0)
    _sold(hist_db, "2026-10-02", "Joplin", bb.MIN_PRINT_HEAD - 1, 400.0)
    _sold(hist_db, "2026-10-02", "Carthage", 100, 400.0)
    got = [r["date"] for r in bb.load_barn_history("Joplin", "MO", 700)]
    assert got == ["2026-10-01"], got


def test_it_is_HEAD_weighted_like_the_rest_of_this_page(hist_db):
    """
    The tiles and the table on this tab are head-weighted and say so.
    cash_calves.py is pound-weighted for its own reasons; borrowing that
    formula here would draw a line that disagrees with the number directly
    above it.

    Two lots at the same barn, equal head, very different weights: pound
    weighting would pull the average toward the heavier lot.
    """
    c = sqlite3.connect(hist_db)
    c.executemany("INSERT INTO calf_sales VALUES (?,?,?,?,?,?,?)", [
        ("2026-10-01", "Joplin", "MO", 700, 100, 500.0, 400.0),
        ("2026-10-01", "Joplin", "MO", 700, 100, 900.0, 300.0),
        ("2026-10-01", "Carthage", "MO", 700, 100, 725.0, 350.0)])
    c.commit()
    c.close()
    got = bb.load_barn_history("Joplin", "MO", 700)
    assert abs(got[0]["price"] - 350.0) < 1e-9, (
        "head-weighting gives a flat 350; got %r" % got[0]["price"])
    # pound-weighted would be (500*400 + 900*300) / (500+900) = 335.71
    assert abs(got[0]["price"] - 335.714) > 1.0


def test_a_barn_that_never_sold_at_this_weight_returns_nothing(hist_db):
    _sold(hist_db, "2026-10-01", "Carthage", 100, 400.0)
    assert bb.load_barn_history("Joplin", "MO", 700) == []


def test_a_missing_table_degrades_quietly(monkeypatch):
    """The rest of this module returns empty rather than raising on a backend
    without the table; a chart must not be the one thing that breaks the tab."""
    monkeypatch.setattr(bb, "_conn", lambda: (_ for _ in ()).throw(RuntimeError))
    assert bb.load_barn_history("Joplin", "MO", 700) == []


def test_the_chart_does_not_follow_the_vs_FCI_toggle():
    """
    A DELIBERATE ASYMMETRY, pinned so it is not "fixed" later.

    calf_sales starts 2025-09-01 and INDEX_TRUSTWORTHY_FROM is 2026-08-28, so a
    line against the index could honestly begin no earlier than the cutoff --
    39 sale days exist on or after it, and the best-covered barn has 13. The
    chart is therefore always barn-against-bracket, and the caption says so
    rather than letting the radio above imply it switched.
    """
    import pathlib
    src = pathlib.Path(bb.__file__).read_text(encoding="utf-8")
    body = src[src.index("def load_barn_history"):]
    body = body[:body.index("\ndef ")]
    assert "fci_daily" not in body, (
        "load_barn_history now reads fci_daily; if the chart is being pointed "
        "at the index, check how many sale days survive INDEX_TRUSTWORTHY_FROM "
        "before trusting the line")
    assert bb.INDEX_TRUSTWORTHY_FROM.isoformat() == "2026-08-28"


def test_the_line_BREAKS_where_the_barn_stopped_selling():
    """
    A line drawn straight across a gap invents trades that did not happen.

    A barn sells weekly -- the median spacing between one barn's sale days at a
    bracket is 7 days. Billings MT has a gap of 105 days in the live data,
    2026-05-28 to 2026-09-10, and joining it would draw three and a half months
    of continuous trading that never occurred. El Reno and Beaver have no gap
    over 21 days at all, so their lines must stay unbroken -- a rule that
    chopped every series would be as wrong in the other direction.

    This pins the rule, not the rendering: the chart passes connectgaps=False
    and a None at each break, which is only meaningful if the breaks are in the
    right places.
    """
    import datetime as dt
    gap = bb.MAX_LINE_GAP_DAYS

    def breaks(days):
        x = [dt.date.fromisoformat(d) for d in days]
        return [i for i in range(1, len(x)) if (x[i] - x[i - 1]).days > gap]

    weekly = ["2026-09-%02d" % d for d in (1, 8, 15, 22, 29)]
    assert breaks(weekly) == [], "a weekly seller was chopped up"

    # the Billings shape: weekly, then a 105-day silence, then weekly again
    stopped = ["2026-05-21", "2026-05-28", "2026-09-10", "2026-09-17"]
    assert breaks(stopped) == [2], (
        "the 105-day silence is still being drawn as a straight line")

    # exactly on the threshold is not a break; one day past it is
    assert breaks(["2026-09-01", "2026-09-01"]) == []
    on = dt.date(2026, 9, 1) + dt.timedelta(days=gap)
    past = dt.date(2026, 9, 1) + dt.timedelta(days=gap + 1)
    assert breaks(["2026-09-01", on.isoformat()]) == []
    assert breaks(["2026-09-01", past.isoformat()]) == [1]


def test_the_stats_in_the_caption_ignore_the_drawing_breaks():
    """
    The breaks are a drawing decision, not missing observations. If the None
    placeholders leaked into the premium list the median and the percentage
    above the bracket would both be computed over a different population than
    the one the caption claims.
    """
    import pathlib
    src = pathlib.Path(bb.__file__).read_text(encoding="utf-8")
    i = src.index("_gp = [a - b for a, b in zip(_bp, _kp)]")
    tail = src[i:i + 4000]
    assert "_med = sorted(_gp)[len(_gp) // 2]" in tail, (
        "the median premium is no longer taken from the unbroken list")
    assert "sum(1 for g in _gp if g > 0)" in tail, (
        "the share above the bracket is no longer taken from the unbroken list")


@pytest.mark.parametrize("name", ["barn_basis.py", "cash_calves.py"])
def test_no_raw_placeholder_survives_into_a_dual_backend_query(name):
    """
    A LITERAL "?" IS A BUG THAT ONLY APPEARS IN PRODUCTION.

    These modules run on SQLite locally and Snowflake on the deployed app, and
    the two take different placeholder styles -- "?" against "%s". A bound "?"
    passes every test here, works in every local preview, and raises on the
    deployed backend, where the module's own `except Exception: return []`
    swallows it and the feature simply never appears.

    That is exactly what happened to load_barn_history on 2026-10-23: two
    reboots and a verified-live check before the cause was found, because the
    page looked identical to one where the chart had not been written yet.

    Both modules interpolate instead -- `f"WHERE weight_low = {int(wt)}"` --
    which is why neither needs a placeholder at all. Where binding IS wanted,
    snowflake_db.placeholders(n) returns the right style per backend, as
    composition.py and snapshots.py do.

    This checks the SQL, not the prose: a "?" inside a docstring is fine and
    barn_basis has one.
    """
    import ast
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / name).read_text(
        encoding="utf-8")
    tree = ast.parse(src)

    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            d = ast.get_docstring(node, clean=False)
            if d:
                docstrings.add(d)

    bad = [(n.lineno, n.value) for n in ast.walk(tree)
           if isinstance(n, ast.Constant) and isinstance(n.value, str)
           and "?" in n.value and n.value not in docstrings]
    assert not bad, (
        "%s has a raw '?' placeholder in a query string: %s. It will work "
        "locally and return nothing on Snowflake. Interpolate the value or "
        "use snowflake_db.placeholders()." % (name, bad))

    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "execute" and len(n.args) > 1]
    assert not calls, (
        "%s binds parameters at line(s) %s. Neither module does this "
        "elsewhere; if it is wanted, the placeholder string must come from "
        "snowflake_db.placeholders() so it is right on both backends."
        % (name, [c.lineno for c in calls]))
