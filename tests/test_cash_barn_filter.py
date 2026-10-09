"""
The Cash Feeder Prices tab lists every barn, so unlike the Sale Barn Basis
chart it hides nothing -- what it does instead is bury. 79 barns across all
states, 21 in Missouri alone at 600-649 lb over three months, and a reader who
follows four of them has to find them in that list every time.

The barn multiselect filters the table down to the picked barns, AND the tiles
and the spread with it. That second half is the risk this file exists for: the
headline tile is labelled with what it is averaging, so a filter that moved the
label without moving the number -- or moved the number with the wrong weighting
-- would put a figure under a name it does not belong to. CLAUDE.md records
exactly that failure on the basis toggle, where the control said one thing and
the maths did another.

So `summarise` is checked against `load_rows`'s OWN summary on real data rather
than against hand arithmetic, in both directions: the whole roster must come
back bit-for-bit, and a subset must not.
"""
import sqlite3
import sys
import types

import pytest

# The streamlit stub, for the reason spelled out in test_barn_basis_pins.py:
# load_rows is wrapped in st.cache_data, and whichever test module imports this
# one first decides whether that cache is real for the entire session.
if "streamlit" not in sys.modules:
    _stub = types.ModuleType("streamlit")
    _stub.cache_data = lambda **kw: (lambda f: f)
    sys.modules["streamlit"] = _stub

import cash_calves as cc


def _rows(*specs):
    """(barn, head, price, weight) -> the row shape load_rows returns."""
    return [{"barn": b, "state": "MO", "head": h, "price": p, "weight": w,
             "last": "2026-10-07", "prints": 3}
            for b, h, p, w in specs]


def test_summarise_is_POUND_weighted_not_head_weighted():
    """
    The two diverge whenever the picked barns sell different weights, which is
    the normal case. Head-weighting here would silently disagree with the
    $/cwt column it sits above -- that column is pound-weighted.
    """
    rows = _rows(("A", 100, 400.0, 500.0), ("B", 100, 300.0, 900.0))
    got = cc.summarise(rows)["price"]
    pounds = (100 * 500 * 400 + 100 * 900 * 300) / (100 * 500 + 100 * 900)
    head = (100 * 400 + 100 * 300) / 200
    assert got == pytest.approx(pounds)
    assert got != pytest.approx(head), \
        "pound- and head-weighting must actually differ on this fixture, or " \
        "the test cannot tell them apart"


def test_summarise_counts_the_picked_barns_not_all_of_them():
    rows = _rows(("A", 10, 400.0, 600.0), ("B", 20, 410.0, 600.0),
                 ("C", 30, 420.0, 600.0))
    assert cc.summarise(rows)["barns"] == 3
    assert cc.summarise(rows[:2])["barns"] == 2


def test_summarise_head_and_weight_are_the_subsets_own():
    rows = _rows(("A", 10, 400.0, 500.0), ("B", 30, 400.0, 700.0))
    got = cc.summarise(rows)
    assert got["head"] == 40
    assert got["weight"] == pytest.approx((10 * 500 + 30 * 700) / 40)


def test_summarise_takes_the_latest_sale_in_the_subset():
    rows = _rows(("A", 10, 400.0, 600.0), ("B", 10, 400.0, 600.0))
    rows[0]["last"] = "2026-09-01"
    rows[1]["last"] = "2026-10-07"
    assert cc.summarise(rows)["last"] == "2026-10-07"
    assert cc.summarise(rows[:1])["last"] == "2026-09-01"


def test_summarise_returns_None_on_an_empty_subset():
    assert cc.summarise([]) is None


def test_summarise_returns_None_rather_than_dividing_by_zero():
    """A barn row carrying no head must not take the page down."""
    assert cc.summarise(_rows(("A", 0, 400.0, 600.0))) is None


# --- the real-data half: filtering to everything must change nothing ---

SCHEMA = """
CREATE TABLE calf_sales (
    report_date TEXT, slug_id INTEGER, location TEXT, state TEXT,
    weight_low INTEGER, weight_high INTEGER, muscle_grade TEXT,
    head_count INTEGER, avg_weight REAL, avg_price REAL);
"""


class FakeDb:
    def use_snowflake(self):
        return False

    @staticmethod
    def iso(v):
        return v


@pytest.fixture
def db(monkeypatch, tmp_path):
    monkeypatch.delenv("USE_SNOWFLAKE", raising=False)
    path = tmp_path / "t.db"
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    # Deliberately uneven: different head counts AND different average weights,
    # so pound- and head-weighting cannot coincide by accident.
    for i, (loc, head, wt, price) in enumerate([
            ("Green City", 150, 612.0, 402.10),
            ("Unionville", 40, 588.5, 395.75),
            ("Carthage", 900, 630.0, 388.40),
            ("Kingdom City", 310, 601.0, 410.00)]):
        con.execute(
            "INSERT INTO calf_sales VALUES (date('now','-5 day'),?,?,'MO',"
            "600,650,'1',?,?,?)", (i, loc, head, wt, price))
    con.commit()
    con.close()
    monkeypatch.setattr(cc, "_conn", lambda: (FakeDb(), sqlite3.connect(path)))
    return path


def test_filtering_to_EVERY_barn_reproduces_the_unfiltered_headline(db):
    """
    The load-bearing one. If re-aggregating the rows does not collapse back to
    load_rows's own figure, then merely opening the picker and selecting all
    would move a published price.
    """
    rows, summ = cc.load_rows(600, cc.ALL_STATES, 30)
    assert len(rows) == 4
    again = cc.summarise(rows)
    assert again["price"] == pytest.approx(summ["price"], abs=1e-12)
    assert again["weight"] == pytest.approx(summ["weight"], abs=1e-12)
    assert again["head"] == summ["head"]
    assert again["barns"] == summ["barns"]
    assert again["last"] == summ["last"]


def test_a_subset_really_does_move_the_headline(db):
    """
    Guard the guard. If a subset read the same as the whole roster, the test
    above would pass against a `summarise` that ignored its argument.
    """
    rows, summ = cc.load_rows(600, cc.ALL_STATES, 30)
    sub = [r for r in rows if r["barn"] in ("Green City", "Kingdom City")]
    assert len(sub) == 2
    assert cc.summarise(sub)["price"] != pytest.approx(summ["price"])
    assert cc.summarise(sub)["head"] < summ["head"]


def test_the_subset_price_is_the_one_a_hand_query_gives(db):
    """Independently derived, per CLAUDE.md -- not recomputed from the module."""
    rows, _ = cc.load_rows(600, cc.ALL_STATES, 30)
    sub = [r for r in rows if r["barn"] in ("Green City", "Carthage")]
    want = ((150 * 612.0 * 402.10 + 900 * 630.0 * 388.40)
            / (150 * 612.0 + 900 * 630.0))
    assert cc.summarise(sub)["price"] == pytest.approx(want)


# --- the roster-churn guard ---

def test_keep_selected_drops_a_barn_the_state_box_filtered_away():
    """
    The loud case: four Missouri barns picked, then State switched to NE. Every
    pick is stale at once, and st.multiselect raises on a default that is not
    among its options.
    """
    nebraska = ["Bassett NE", "Ericson NE"]
    picks = ["Green City MO", "Carthage MO"]
    assert cc.keep_selected(picks, nebraska) == []


def test_keep_selected_keeps_the_survivors():
    kept = cc.keep_selected(["Green City MO", "Unionville MO"],
                            ["Carthage MO", "Green City MO"])
    assert kept == ["Green City MO"]


def test_keep_selected_returns_roster_order_and_tolerates_nothing_picked():
    assert cc.keep_selected(["C", "A"], ["A", "B", "C"]) == ["A", "C"]
    assert cc.keep_selected(None, ["A"]) == []
    assert cc.keep_selected([], ["A"]) == []


def test_the_label_matches_barn_basis_exactly():
    """
    Same barn, same name on both tabs, so a reader can carry one across. The
    two modules define _label separately on purpose; this is what stops them
    drifting apart.
    """
    import barn_basis as bb
    r = {"barn": "Green City", "state": "MO"}
    assert cc._label(r) == bb._label(r) == "Green City MO"
