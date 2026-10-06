"""
Preliminary reports are in the sample, and a revision replaces what it revises.

WHY PRELIMINARY IS ADMITTED (2026-10-06). Oklahoma National Stockyards (slug
1280) files Preliminary on the sale day and Finals later, so its ~1,100 Monday
head reached the index only after the morning call -- late on 09-14, 09-21,
09-28 and 10-05, four Mondays running, each about 24% of a typical Monday's
pounds. On 10-05 a scan of all 89 roster slugs found it was the ONLY barn held
back by the flag; every other barn with AMS data that day was Final and already
held, head for head. So the filter accepts Preliminary generally rather than
allow-listing one slug: identical behaviour today, and no special case to be
wrong the first time a second barn does this.

WHY THAT NEEDED THE MERGE FIXED FIRST. mars_sales was written insert-if-absent
with avg_price and head_count IN the key, so a revised lot arrived as a SECOND
row and the first stayed. McAlester OK published 15 head at $328.32, corrected
it to 14 at $330.29, and we held five lots where AMS served four; Mitchell SD
withdrew a 6-head lot at $275.00 and it sat in the published index for five
days. Both were found by mars_census, long after they had moved the number.

That was survivable while every row came from a FINAL report. Preliminary rows
are precisely the ones AMS revises, so admitting them under the old merge would
have made the double count routine instead of rare. store_slug_rows() replaces
a slug's rows for a date; these tests are what say it still does.
"""
import sqlite3

import pytest

import update_index as ui


def _row(**kw):
    """
    One AMS row in the shape the payload really has: weight_break_low is an
    INT (TARGET_BRACKETS is a set of ints) while muscle_grade is a string.

    The first version of this file used "750" and every row silently failed to
    qualify -- which made the two acceptance tests fail loudly and, worse, made
    the four REJECTION tests pass for the wrong reason, asserting an empty list
    against an empty list. test_the_base_row_actually_qualifies below is what
    stops that happening again.
    """
    r = {"class": "Steers", "frame": "Medium and Large", "muscle_grade": "1",
         "weight_break_low": 750, "head_count": 100, "avg_weight": 780.0,
         "avg_price": 340.00, "final_ind": "Final",
         "report_date": "10/05/2026", "report_narrative": None}
    r.update(kw)
    return r


def test_the_base_row_actually_qualifies():
    """
    GUARD THE FIXTURE. Every rejection test below asserts an empty result, so
    they all pass trivially if the base row stops qualifying for an unrelated
    reason -- a renamed field, a type change in the payload. This is the one
    test that fails when that happens.
    """
    assert len(ui.qualifying_rows([_row()])) == 1,         "the base row must qualify, or the rejection tests prove nothing"


# ---------------------------------------------------------------------------
# The filter.
# ---------------------------------------------------------------------------

def test_preliminary_rows_are_in_the_sample():
    """The 2026-10-06 change. Oklahoma City's Monday rows are Preliminary."""
    assert len(ui.qualifying_rows([_row(final_ind="Preliminary")])) == 1


def test_final_rows_are_still_in_the_sample():
    assert len(ui.qualifying_rows([_row(final_ind="Final")])) == 1


def test_no_other_final_ind_value_is_admitted():
    """
    Accepting Preliminary is not accepting ANYTHING. AMS also files revisions
    and corrections under their own markers, and a blanket `!= None` would have
    swept in whatever it invents next.
    """
    for bad in ("Revised", "Corrected", "Estimated", "", None, "preliminary"):
        assert ui.qualifying_rows([_row(final_ind=bad)]) == [], repr(bad)


def test_the_other_rule_10203_filters_are_untouched():
    """Admitting preliminary must not have loosened anything else."""
    assert ui.qualifying_rows([_row(**{"class": "Dairy Steers"})]) == []
    assert ui.qualifying_rows([_row(frame="Small")]) == []
    assert ui.qualifying_rows([_row(muscle_grade="2")]) == []
    assert ui.qualifying_rows([_row(weight_break_low="650")]) == []
    assert ui.qualifying_rows([_row(weight_break_low="900")]) == []


# ---------------------------------------------------------------------------
# The merge: a revision must replace, not accumulate.
# ---------------------------------------------------------------------------

LOC = {"city": "Oklahoma City", "title": "Oklahoma National Stockyards",
       "state": "OK"}


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.delenv("USE_SNOWFLAKE", raising=False)
    c = sqlite3.connect(":memory:")
    c.execute("""CREATE TABLE mars_sales (
        report_date TEXT, raw_date TEXT, slug_id INTEGER, location TEXT,
        state TEXT, weight_low INTEGER, muscle_grade TEXT, head_count INTEGER,
        avg_weight REAL, avg_price REAL, published_date TEXT)""")
    return c


def _stored(c):
    return c.execute("SELECT COUNT(*), COALESCE(SUM(head_count),0) "
                     "FROM mars_sales").fetchone()


def test_a_revision_replaces_the_row_it_revises(conn):
    """
    THE McALESTER CASE, which cost three index dates. AMS published 15 head at
    $328.32 and corrected it to 14 at $330.29. Under the old insert-if-absent
    merge both survived, because avg_price and head_count are in the key.
    """
    ui.store_slug_rows(conn, 1827, LOC,
                       [_row(head_count="15", avg_price="328.32")])
    assert _stored(conn) == (1, 15)

    ui.store_slug_rows(conn, 1827, LOC,
                       [_row(head_count="14", avg_price="330.29")])
    assert _stored(conn) == (1, 14), \
        "the correction must REPLACE the row it corrects, not sit beside it"


def test_a_withdrawn_lot_disappears(conn):
    """
    THE MITCHELL CASE. AMS served four qualifying lots in the morning and three
    that afternoon; the phantom fourth -- 6 head at $275.00 against that barn's
    own $336-353 -- stayed in the published index for five days.
    """
    ui.store_slug_rows(conn, 2022, LOC, [
        _row(weight_break_low="700", head_count="45"),
        _row(weight_break_low="750", head_count="65"),
        _row(weight_break_low="800", head_count="117"),
        _row(weight_break_low="850", head_count="6", avg_price="275.00")])
    assert _stored(conn) == (4, 233)

    ui.store_slug_rows(conn, 2022, LOC, [
        _row(weight_break_low="700", head_count="45"),
        _row(weight_break_low="750", head_count="65"),
        _row(weight_break_low="800", head_count="117")])
    assert _stored(conn) == (3, 227), "the withdrawn lot must be gone"


def test_an_empty_fetch_deletes_nothing(conn):
    """
    THE SAFETY PROPERTY, and the reason this is a replace and not a truncate.
    An API hiccup, a rate limit, or a report AMS has not posted yet all arrive
    as zero rows. Treating that as "the day is empty" would wipe a day we
    already hold -- turning a transient fetch failure into data loss.

    NOTE WHAT THIS DOES AND DOES NOT PIN. The property is structural: the
    deletes are driven off a dict built from qrows, so no rows means no DELETE
    runs, and store_slug_rows' early return is redundant -- removing it leaves
    every test here green. What this test actually catches is a blanket
    "DELETE ... WHERE slug_id = ?" outside the loop, which is the hazard worth
    guarding. Verified by adding one: this test and the scoping test below
    both fail.
    """
    ui.store_slug_rows(conn, 1280, LOC, [_row(head_count="1168")])
    assert _stored(conn) == (1, 1168)
    ui.store_slug_rows(conn, 1280, LOC, [])
    assert _stored(conn) == (1, 1168), "an empty fetch must not delete anything"


def test_the_replace_is_scoped_to_one_slug_and_one_date(conn):
    """
    Never a blanket delete. Another barn's rows for the same date, and this
    barn's rows for another date, must both survive.
    """
    ui.store_slug_rows(conn, 1280, LOC, [_row(head_count="1168")])
    ui.store_slug_rows(conn, 1245, dict(LOC, city="Carthage"),
                       [_row(head_count="1501")])
    ui.store_slug_rows(conn, 1280, LOC,
                       [_row(head_count="1079", report_date="09/28/2026")])
    assert _stored(conn) == (3, 1168 + 1501 + 1079)

    # re-filing 10/05 replaces only 10/05 for slug 1280
    ui.store_slug_rows(conn, 1280, LOC, [_row(head_count="1200")])
    n, head = _stored(conn)
    assert n == 3 and head == 1200 + 1501 + 1079, \
        "the replace reached rows it should not have"


def test_preliminary_then_final_leaves_one_row(conn):
    """
    The sequence this change exists for, end to end: Oklahoma City files
    Preliminary on the sale day and Final later the same morning. The index
    must see 1,168 head either way -- never 2,336.
    """
    prelim = [_row(head_count="1168", avg_price="335.50",
                   final_ind="Preliminary")]
    ui.store_slug_rows(conn, 1280, LOC, prelim)
    assert _stored(conn) == (1, 1168)

    final = [_row(head_count="1168", avg_price="335.52", final_ind="Final")]
    ui.store_slug_rows(conn, 1280, LOC, final)
    n, head = _stored(conn)
    assert (n, head) == (1, 1168), \
        "preliminary and final counted twice -- the exact double count this fixes"
    assert conn.execute("SELECT avg_price FROM mars_sales").fetchone()[0] == 335.52, \
        "the FINAL price must win"
