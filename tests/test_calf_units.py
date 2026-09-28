"""
Every price in calf_sales must be $/cwt, including the ones AMS quotes per head.

WHY THIS IS A TEST. The ingest filtered on class, frame, muscle grade, weight
break, final_ind, head count, weight and price -- and never on price_unit, the
field AMS sends on every row saying what the price MEANS. Per-animal lots
therefore landed in a column every reader treats as hundredweight. Nothing
raised. The table simply held, for eleven months, a scattering of rows like
Dunlap IA on 2026-08-07: 35 head at 442 lb, $2,153.40 A HEAD, stored as
$2,153.40/cwt and enough on its own to make Dunlap the leading barn on the
basis chart at $1,118.70/cwt.

The rows below are REAL, copied from live MARS payloads on 2026-09-18, not
invented. Dunlap is the case that prompted the fix. Dodge City is the case that
matters more, because it is the one a price threshold misses: $825.00 per head
on a 527 lb calf is $156.55/cwt, wrong in exactly the same way and well inside
the range a genuine $/cwt price occupies. A magnitude check would have passed
it. Checking the unit is what catches both.
"""
import pytest

import calf_sales as cs


def ams_row(**kw):
    """A MARS auction row that passes the calf filter, with fields overridden."""
    row = {"class": "Steers", "frame": "Medium and Large", "muscle_grade": "1",
           "final_ind": "Final", "report_date": "08/07/2026",
           "weight_break_low": 400, "weight_break_high": 450,
           "head_count": 35, "avg_weight": 442, "avg_price": 2153.40,
           "price_unit": "Per Unit", "freight": "F.O.B.", "lot_desc": "None",
           "report_narrative": None}
    row.update(kw)
    return row


# Dunlap Livestock Auction, Dunlap IA, 2026-08-07. Verified against slug 2155.
DUNLAP_PER_HEAD = ams_row()

# Dodge City KS, 2025-12-17, slug-verified. The row a price threshold misses.
DODGE_CITY_PER_HEAD = ams_row(report_date="12/17/2025", weight_break_low=500,
                              weight_break_high=550, head_count=4,
                              avg_weight=527, avg_price=825.00)


def cwt_price(row):
    """Run one row through the real ingest path and return what would be stored."""
    kept, rejected = cs.on_a_cwt_basis(cs.qualifying_calf_rows([row]))
    assert not rejected, f"unexpectedly rejected: {rejected}"
    assert len(kept) == 1, f"expected one row, got {len(kept)}"
    return kept[0]["avg_price"]


class TestPerHeadIsConverted:
    def test_the_dunlap_row_that_caused_this(self):
        """$2,153.40 a head on 442 lb cattle is $487.19/cwt."""
        assert cwt_price(DUNLAP_PER_HEAD) == pytest.approx(487.19, abs=0.01)

    def test_the_row_a_price_threshold_would_have_missed(self):
        """
        $825.00 a head on 527 lb cattle is $156.55/cwt. Under $1,000 either
        way, so no magnitude check finds it -- and this is why the ingest
        converts on the unit rather than on the size of the number.
        """
        assert cwt_price(DODGE_CITY_PER_HEAD) == pytest.approx(156.55, abs=0.01)

    def test_both_spellings_of_the_per_animal_unit_convert(self):
        """
        AMS renamed "Per Head" to "Per Unit" in 2022. Honouring only the
        current label is what silently dropped 22,709 replacement rows.
        """
        per_unit = cwt_price(ams_row(price_unit="Per Unit"))
        per_head = cwt_price(ams_row(price_unit="Per Head"))
        assert per_unit == per_head == pytest.approx(487.19, abs=0.01)

    def test_a_per_cwt_row_is_left_exactly_alone(self):
        """The common case must pass through untouched, not round-trip."""
        row = ams_row(price_unit="Per Cwt", avg_price=487.19)
        assert cwt_price(row) == 487.19

    def test_the_caller_s_payload_is_not_mutated(self):
        """
        A converted row is a copy. ingest() holds AMS's dict and the whole
        point of storing raw_avg_price is that the original is still knowable.
        """
        row = ams_row()
        kept, _ = cs.on_a_cwt_basis(cs.qualifying_calf_rows([row]))
        assert row["avg_price"] == 2153.40
        assert kept[0]["raw_avg_price"] == 2153.40
        assert kept[0]["avg_price"] == pytest.approx(487.19, abs=0.01)


class TestTheConversionIsWhatMakesThoseTrue:
    """
    Guard the guard. Three checks written in this project could not fail -- one
    printed the same variable under both labels, one was a compound tautology,
    one banned a string in comments rather than in queries -- so a check here
    has to be shown failing on bad input before it is worth anything.

    These reproduce the bug through the module's own machinery and assert the
    tests above would go red.
    """

    def test_ignoring_the_unit_puts_the_per_head_price_back(self, monkeypatch):
        """
        The bug as it stood: the unit read as though it already meant $/cwt.
        Nothing is rejected, nothing converts, and $2,153.40 a head is stored
        as $2,153.40/cwt.
        """
        monkeypatch.setattr(cs, "PER_CWT_UNIT", "Per Unit")
        assert cwt_price(DUNLAP_PER_HEAD) == pytest.approx(2153.40)

    def test_and_that_value_fails_the_assertion_above(self, monkeypatch):
        """So the 487.19 check discriminates; it is not true by construction."""
        monkeypatch.setattr(cs, "PER_CWT_UNIT", "Per Unit")
        with pytest.raises(AssertionError):
            assert cwt_price(DUNLAP_PER_HEAD) == pytest.approx(487.19, abs=0.01)

    def test_dividing_by_the_wrong_thing_also_fails_it(self):
        """
        Belt and braces on the arithmetic itself: weight/100, not weight, and
        not head count. Both plausible slips, both caught.
        """
        assert 2153.40 / 442 != pytest.approx(487.19, abs=0.01)
        assert 2153.40 / 35 != pytest.approx(487.19, abs=0.01)


class TestUnconvertibleRowsAreDroppedLoudly:
    @pytest.mark.parametrize("unit", ["Per Family", "Per Bushel", "", None])
    def test_an_unconvertible_unit_is_rejected_not_stored(self, unit):
        """
        A price on an unknown basis is worse than no price: it looks exactly
        like a real one. "Per Family" is a cow AND her calf, so the cow's
        weight cannot divide it; an empty or novel label is simply unknown.
        """
        kept, rejected = cs.on_a_cwt_basis(
            cs.qualifying_calf_rows([ams_row(price_unit=unit)]))
        assert kept == [], f"{unit!r} was stored anyway"
        assert len(rejected) == 1
        reason, row = rejected[0]
        assert "price_unit=" in reason
        assert row["avg_price"] == 2153.40

    def test_per_family_is_not_quietly_folded_into_per_head(self):
        """The distinction replacement_reports.py draws must hold here too."""
        assert not (cs.PER_PAIR_UNITS & cs.PER_HEAD_UNITS)
        assert cs.PER_CWT_UNIT not in cs.PER_HEAD_UNITS

    def test_a_per_animal_price_with_no_weight_cannot_be_converted(self):
        """Nothing to divide by, so the row goes rather than guessing."""
        kept, rejected = cs.on_a_cwt_basis([ams_row(avg_weight=0)])
        assert kept == []
        assert "no weight" in rejected[0][0]


def test_the_stored_table_holds_no_per_animal_prices():
    """
    The data, not the code. A coarse tripwire -- the guarantee is the unit
    filter above, and this only catches contamination big enough to show as an
    impossible $/cwt price. It is here because it is the check that actually
    found the bug, and it should stay able to find it again.
    """
    from pathlib import Path
    db_path = Path(__file__).resolve().parent.parent / "data" / "mars_history.db"
    if not db_path.exists():
        pytest.skip("no local database")
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        bad = conn.execute(
            "SELECT report_date, location, weight_low, head_count, avg_weight, "
            "avg_price FROM calf_sales WHERE avg_price > 1000 "
            "ORDER BY avg_price DESC LIMIT 5").fetchall()
    except sqlite3.OperationalError:
        pytest.skip("calf_sales absent")
    finally:
        conn.close()
    assert not bad, (
        f"calf_sales holds {len(bad)} row(s) priced above $1,000/cwt, e.g. "
        f"{bad[0]}. That is a per-animal price in a $/cwt column -- re-run "
        f"calf_sales.py --since to re-ingest through on_a_cwt_basis().")
