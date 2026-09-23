"""
Every constant and every load-bearing expression in barn_report.py, each with a
test that DIES when it is mutated.

This module is the third rewrite of the barn report. The first two were reverted
for the same reason both times: a formula or a threshold could be changed --
abs() dropped from a spread, a share denominator swapped -- and the suite stayed
green. The fix was to stop having knobs. What is left is two constants and about
half a dozen expressions, which is few enough that each can be pinned here by
name.

Each mutation below was applied to a SANDBOX COPY of barn_report.py, never the
tracked file, and the named test watched to fail before it was trusted:

    mutation                                  first test that dies
    --------------------------------------    ------------------------------
    MIN_PRESENT 9 -> 10                       ..missed_three_occurrences_is_still_expected
    MIN_PRESENT 9 -> 8                        ..missed_four_occurrences_has_aged_off
    OCCURRENCES 12 -> 11                      ..missed_three_occurrences_is_still_expected
    OCCURRENCES 12 -> 13                      ..missed_four_occurrences_has_aged_off
    range(1, N + 1) -> range(0, N)            ..index_date_itself_does_not_count_toward_expected
    walk anchored on the week's Monday        ..expected_reads_the_index_dates_own_weekday
      instead of on index_date                ..a_thursday_index_date_is_judged_against_thursdays
    shifted_bucket_date(loc, d) -> d          ..buckets_the_way_the_index_counts
    barn_days keyed on location               ..is_keyed_on_slug_id_through_the_database
                                              ..one_slug_reporting_does_not_cover_its_twin
    barns[slug] = ... instead of accumulating ..accumulates_every_row_in_a_bucket
    median -> mean, -> max, and -> min        ..pounds_are_the_median_of_the_occurrences
    expected() collects head, not pounds      ..expected_measures_pounds_not_head
    sort reversed, and sort dropped           ..missing_barns_rank_on_pounds_not_head
    missing filtered to a share of the day,   ..every_missing_barn_prints_however_small
      at 10% or at 1%
    detail rows capped -- rows[:3], rows[:1]  ..every_missing_barn_prints_however_small
    `not in reported` -> `in reported`        ..a_complete_day_prints_one_line
    presence read off index_date - 1          ..a_complete_day_prints_one_line
    share denominator -> missing barns only   ..share_is_of_every_expected_barns_pounds
    fci_daily dropped from `available`        ..day_with_no_sales_is_reported_not_skipped
    index date -> max(available)              ..index_date_is_cmes_publication_clock
    MAX(report_date) -> MIN(report_date)      ..index_date_is_cmes_newest_file_not_its_oldest
    `if row and row[0]` -> `if row`           ..an_empty_cme_table_is_not_a_published_date
    _names assigns instead of setdefault,     ..a_barns_name_is_its_first_spelling
      and its ORDER BY reversed or dropped
    _index_date's except -> raise             ..index_date_falls_back_when_cmes_table_is_missing
    report_lines' except -> raise             ..report_lines_cannot_raise
    report_lines returns a generator          ..the_header_counts_the_barns_that_did_report
    loop outside the guard in update_index    ..update_index_consumes_the_report_inside_a_guard

NO NUMBER IN A COMMENT THAT A TEST DOES NOT PRODUCE. Two rounds of review
findings here were comments asserting measurements that did not reproduce. Every
figure below is one these fixtures compute.

ONE FIXTURE SHAPE IS NOT A PROPERTY. Six of the mutations above survived an
earlier round of this file for the same reason: every fixture chose the same
shape, so a mutation that only matters on a different shape had nothing to
fail. Every index date was a MONDAY, so "same weekday" was never a claim;
cme_ftp_daily held exactly one row everywhere, so MAX and MIN were the same
number; the median fixture's minimum WAS its median. Vary the shape, not just
the values.
"""
import ast
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

import barn_report as br

REPO = Path(__file__).resolve().parent.parent

SCHEMA = """
CREATE TABLE mars_sales (
    report_date TEXT, raw_date TEXT, slug_id INTEGER, location TEXT,
    state TEXT, weight_low INTEGER, muscle_grade TEXT,
    head_count INTEGER, avg_weight REAL, avg_price REAL, published_date TEXT
);
CREATE TABLE fci_daily (report_date TEXT, fci_value REAL);
CREATE TABLE cme_ftp_daily (report_date TEXT, fci_value REAL);
"""

MONDAY = date(2026, 9, 21)
THURSDAY = date(2026, 9, 17)


@pytest.fixture(autouse=True)
def _sqlite_backend(monkeypatch):
    """These fixtures are sqlite; db.placeholders() must not emit %s."""
    monkeypatch.delenv("USE_SNOWFLAKE", raising=False)


def make_conn(rows=(), published=None, extra_index_dates=()):
    """
    mars_sales holding exactly `rows` -- (iso date, slug_id, city, state, head,
    avg weight) -- plus the two tables the index date is picked from.

    A row may carry two more fields, (weight_low, muscle_grade), and defaults to
    750/'1' without them. Every fixture wrote that one shape until
    test_barn_days_reads_every_bracket_and_grade, which meant a WHERE clause on
    either column read as correct.

    fci_daily gets a row for every date a sale lands on, which is what
    recompute_fci_daily() does, plus `extra_index_dates` for the days an index
    exists for with no sale of their own.

    `published` is one CME file date or SEVERAL. Take several wherever the test
    is about which end of cme_ftp_daily is read: one row leaves MAX and MIN the
    same value, and the live table is years deep. Passing None leaves the table
    CREATED AND EMPTY, which is its own case -- see
    test_an_empty_cme_table_is_not_a_published_date.
    """
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO mars_sales (report_date, raw_date, slug_id, location, state, "
        "weight_low, muscle_grade, head_count, avg_weight, avg_price) "
        "VALUES (?,?,?,?,?,?,?,?,?,340.0)",
        [(r[0], r[0], r[1], r[2], r[3],
          r[6] if len(r) > 6 else 750, r[7] if len(r) > 7 else "1",
          r[4], r[5]) for r in rows],
    )
    conn.executemany(
        "INSERT INTO fci_daily (report_date, fci_value) VALUES (?, 340.0)",
        [(d,) for d in sorted({r[0] for r in rows} | set(extra_index_dates))],
    )
    if isinstance(published, str):
        published = [published]
    conn.executemany("INSERT INTO cme_ftp_daily (report_date, fci_value) "
                     "VALUES (?, 340.0)", [(d,) for d in published or ()])
    conn.commit()
    return conn


def occurrences(ks, index_date=MONDAY):
    """The same-weekday dates `ks` weeks before index_date, as ISO strings."""
    return [(index_date - timedelta(days=7 * k)).isoformat() for k in ks]


def rows_for(slug, city, dates, head=100, wt=800.0, state="MO"):
    return [(d, slug, city, state, head, wt) for d in dates]


# ---------------------------------------------------------------------------
# barn_days(): the bucket, the key, and the accumulation.
# ---------------------------------------------------------------------------

def test_barn_days_buckets_the_way_the_index_counts():
    """
    El Reno prints Tuesday and the index counts it Wednesday.

    MUTATION: bucket on report_date instead of shifted_bucket_date(). The
    roster then sits one weekday out for every shifted barn -- El Reno becomes
    a Tuesday barn that never reports, and the real Wednesday absence is
    invisible.
    """
    days = br.barn_days(make_conn(rows_for(1, "El Reno", ["2026-09-15"], state="OK")))
    assert list(days) == ["2026-09-16"], "Tuesday's El Reno sale is a Wednesday bucket"


def test_barn_days_is_keyed_on_slug_id_through_the_database():
    """
    Two reports, one city. Driven through barn_days() against a seeded
    database, NOT an injected dict -- an earlier version of this test handed in
    a ready-made slug-keyed mapping, so the mutation it existed to kill never
    touched it.

    MUTATION: key on location. The two rows fuse into one entry and the barn
    that did not report cannot be named.
    """
    conn = make_conn(rows_for(1774, "Billings", ["2026-09-16"], head=10, state="MT")
                     + rows_for(1777, "Billings", ["2026-09-16"], head=20, state="MT"))
    assert br.barn_days(conn)["2026-09-16"] == {1774: (10, 8000.0), 1777: (20, 16000.0)}


def test_barn_days_accumulates_every_row_in_a_bucket():
    """
    A slug contributes several rows to one bucket two ways: several weight
    brackets on one report, and two report dates the bucketing snaps together
    (El Reno's Monday and Tuesday both land on Wednesday).

    MUTATION: assign instead of accumulate. The barn keeps only its last row,
    which understates a large barn and can hide it under a small one.
    """
    conn = make_conn([("2026-09-14", 5, "El Reno", "OK", 40, 700.0),
                      ("2026-09-14", 5, "El Reno", "OK", 30, 800.0),
                      ("2026-09-15", 5, "El Reno", "OK", 20, 900.0)])
    assert br.barn_days(conn) == {
        "2026-09-16": {5: (90, 40 * 700.0 + 30 * 800.0 + 20 * 900.0)}}


def test_barn_days_reads_every_bracket_and_grade():
    """
    The index spans four weight brackets and two muscle grades, and the read
    must total all of them.

    MUTATIONS: any narrowing of the mars_sales read -- WHERE weight_low = 750,
    >= 750, < 800, WHERE muscle_grade = '1', or a LIMIT. Every other fixture
    wrote 750/'1', so each of those read as correct while changing the printed
    line on 14 of 16 September days: on 2026-09-04 the bracket filter deletes a
    genuine missing barn, and on 2026-09-01 the grade filter invents one.
    """
    conn = make_conn([("2026-09-14", 1, "Mitchell", "SD", 10, 700.0, 700, "1"),
                      ("2026-09-14", 1, "Mitchell", "SD", 10, 750.0, 750, "1-2"),
                      ("2026-09-14", 1, "Mitchell", "SD", 10, 800.0, 800, "1"),
                      ("2026-09-14", 1, "Mitchell", "SD", 10, 850.0, 850, "1-2")])
    assert br.barn_days(conn) == {"2026-09-14": {1: (40, 31000.0)}}


# ---------------------------------------------------------------------------
# expected(): the two constants, the window bound, the median, and pounds.
# ---------------------------------------------------------------------------

def test_a_barn_that_missed_three_occurrences_is_still_expected():
    """
    Present on the 9 oldest of the last 12 same-weekday dates -- a barn that
    stopped three weeks ago. It is still expected, and its absence still named.

    MUTATIONS: MIN_PRESENT 9 -> 10 (9 sightings no longer qualify); OCCURRENCES
    12 -> 11 (the oldest sighting falls out of the window, leaving 8).
    """
    days = br.barn_days(make_conn(rows_for(1, "Carthage", occurrences(range(4, 13)))))
    assert 1 in br.expected(days, MONDAY)


def test_a_barn_that_missed_four_occurrences_has_aged_off():
    """
    The same barn one week later: 8 sightings inside the window and a 9th just
    outside it. Four missed occurrences is where a stopped barn stops being
    expected, and the 13th sighting is what pins the far edge of the window.

    MUTATIONS: MIN_PRESENT 9 -> 8 (8 sightings would qualify); OCCURRENCES
    12 -> 13 (the 13th sighting comes back into the window, making 9).
    """
    days = br.barn_days(make_conn(rows_for(1, "Carthage", occurrences(range(5, 14)))))
    assert 1 not in br.expected(days, MONDAY)


def test_the_index_date_itself_does_not_count_toward_expected():
    """
    The index date is the day being judged, so a barn's presence on it must not
    help decide whether it was expected on it.

    MUTATION: range(1, OCCURRENCES + 1) -> range(0, OCCURRENCES). The index
    date becomes a 13th occurrence, this barn reaches 9 sightings, and the
    roster starts including barns on the strength of the very day in question.
    """
    days = br.barn_days(make_conn(
        rows_for(1, "Carthage", occurrences(range(1, 9)) + [MONDAY.isoformat()])))
    assert 1 not in br.expected(days, MONDAY)


def test_expected_reads_the_index_dates_own_weekday():
    """
    "Same weekday" means the INDEX DATE's weekday. It is a property, and every
    other fixture in this file asks about a Monday, where anchoring the walk on
    the index date and anchoring it on that week's Monday are the same thing --
    so the property was never actually claimed.

    One barn sells only Mondays and one only Thursdays. Asked about a Thursday,
    the roster is the Thursday barn; asked about a Monday, the Monday barn.

    MUTATION: walk back from index_date - index_date.weekday() rather than from
    index_date. Every weekday then gets Monday's roster, so a Tuesday, Wednesday
    or Thursday reports against barns that were never going to sell that day --
    a complete Wednesday prints its whole roster as missing.
    """
    assert MONDAY.weekday() == 0 and THURSDAY.weekday() == 3
    days = br.barn_days(make_conn(
        rows_for(1, "Carthage", occurrences(range(1, 13), MONDAY))
        + rows_for(2, "Joplin", occurrences(range(1, 13), THURSDAY))))
    assert set(br.expected(days, THURSDAY)) == {2}, "Thursday's roster is Thursday's barns"
    assert set(br.expected(days, MONDAY)) == {1}, "and Monday's is Monday's barns"


def test_expected_pounds_are_the_median_of_the_occurrences():
    """
    MIN, MEDIAN, MEAN and MAX are four DISTINCT numbers in this fixture, so no
    single-statistic mutation can pass by coincidence. The fixture this
    replaced was eleven equal weeks plus one 10x week, where the minimum and
    the median are the same value -- median -> min survived it, and that is the
    mutation that most understates a big barn.

    MUTATIONS: median -> mean, or -> max (one 10x week sets the barn's size for
    the next twelve); median -> min (the barn is stated at its quietest week,
    which understates the large barns most and reorders the list).
    """
    heads = [40, 55, 70, 85, 100, 115, 130, 145, 160, 175, 190, 900]
    weekly = sorted(head * 800.0 for head in heads)
    lo, mid = weekly[0], (weekly[5] + weekly[6]) / 2
    mean, hi = sum(weekly) / len(weekly), weekly[-1]
    assert len({lo, mid, mean, hi}) == 4, "the fixture must separate all four"
    assert (lo, mid, hi) == (32000.0, 98000.0, 720000.0)
    assert round(mean, 2) == 144333.33

    rows = [(d, 1, "Carthage", "MO", head, 800.0)
            for d, head in zip(occurrences(range(1, 13)), heads)]
    got = br.expected(br.barn_days(make_conn(rows)), MONDAY)[1]
    assert got == mid == 98000.0


def test_expected_measures_pounds_not_head():
    """
    The index is pound-weighted. 95 head at 850 lb outweighs 100 head at 700 lb
    -- both plausible for 700-899 lb feeders -- so head ranks these two barns
    the wrong way round.

    MUTATION: accumulate or rank on head. This is the defect that puts a small
    barn above a large one on the morning the report is read.
    """
    dates = occurrences(range(1, 13))
    conn = make_conn(rows_for(1, "Light", dates, head=100, wt=700.0)
                     + rows_for(2, "Heavy", dates, head=95, wt=850.0))
    roster = br.expected(br.barn_days(conn), MONDAY)
    assert roster == {1: 70000.0, 2: 80750.0}
    assert roster[2] > roster[1], "the heavier barn is the bigger barn"


# ---------------------------------------------------------------------------
# report_lines(): the header, the order, the share, and the promise not to fail.
# ---------------------------------------------------------------------------

def _full_weeks(slugs, index_date=MONDAY, head=100, wt=800.0):
    """Every named barn on all 12 prior occurrences, so all of them qualify."""
    rows = []
    for slug, city in slugs:
        rows += rows_for(slug, city, occurrences(range(1, 13), index_date),
                         head=head, wt=wt)
    return rows


def test_report_lines_builds_the_roster_for_the_index_date_itself():
    """
    The roster must be built for the index date, not a week either side of it.

    MUTATION: expected(days, index_date +/- timedelta(days=7)). A seven-day
    shift preserves the weekday, so the same-weekday test cannot see it, and
    every other report_lines fixture seeds twelve consecutive weeks, which
    leaves the roster unchanged under a shift. Here each barn sits at exactly
    MIN_PRESENT sightings at opposite ends of the window, so the older one falls
    out under +7 and the newer one under -7. Live, a +7 shift drops Giddings
    from the 2026-09-07 roster entirely.
    """
    rows = (rows_for(1, "Oldest", occurrences(range(4, 13)))
            + rows_for(2, "Newest", occurrences(range(1, 10))))
    lines = br.report_lines(make_conn(rows, published="2026-09-18",
                                      extra_index_dates=[MONDAY.isoformat()]))
    assert lines[0].endswith("0 of 2 expected barns reported")
    assert len(lines) == 3, "both barns qualify and neither reported"


def test_a_complete_day_prints_one_line():
    """No missing barn, no detail rows -- the line prints every run regardless."""
    rows = _full_weeks([(1, "Carthage"), (2, "Tulsa")])
    rows += rows_for(1, "Carthage", [MONDAY.isoformat()])
    rows += rows_for(2, "Tulsa", [MONDAY.isoformat()])
    lines = br.report_lines(make_conn(rows, published="2026-09-18"))
    assert lines == ["Barn report -- index date 2026-09-21 (Mon): "
                     "2 of 2 expected barns reported"]


def test_the_header_counts_the_barns_that_did_report():
    rows = _full_weeks([(1, "Carthage"), (2, "Tulsa"), (3, "Joplin")])
    rows += rows_for(3, "Joplin", [MONDAY.isoformat()])
    lines = br.report_lines(make_conn(rows, published="2026-09-18"))
    assert lines[0].endswith("1 of 3 expected barns reported")
    assert len(lines) == 3, "one line per missing barn, uncapped"


def test_every_missing_barn_prints_however_small():
    """
    NO THRESHOLD AND NO CAP -- the one design constraint this rewrite exists to
    hold, and until now the only one nothing pinned. Five barns are missing,
    from one worth three quarters of the day to one that rounds to 0% of it,
    and all five get a line.

    MUTATIONS, all of which passed the earlier suite: filter `missing` to barns
    over 10% of the day; the same at 1%; cap the detail rows at rows[:3]. Each
    deletes the small barns, which is where a quiet morning hides -- and the
    small barn is what prompted this module. A header with the survivors
    counted against the full roster then reads like an ordinary day.

    The lines are asserted verbatim, not just counted, so a cap cannot be
    swapped for a truncation that keeps the line count up. That also pins the
    two column widths, which are computed from the rows actually printed and
    are the only thing keeping a long list readable.
    """
    sizes = [(1, "Bigtop", 2000), (2, "Second", 500), (3, "Third", 100),
             (4, "Fourth", 50), (5, "Tiny", 2)]
    rows = []
    for slug, city, head in sizes:
        rows += _full_weeks([(slug, city)], head=head, wt=800.0)
    lines = br.report_lines(make_conn(rows, published="2026-09-18",
                                      extra_index_dates=[MONDAY.isoformat()]))
    assert lines[0].endswith("0 of 5 expected barns reported")
    assert len(lines) == 1 + 5, "one line per missing barn: no cap, no threshold"
    assert lines[1:] == [
        "  missing: Bigtop MO  ~1,600,000 lb  (~75% of a typical Monday)",
        "  missing: Second MO  ~  400,000 lb  (~19% of a typical Monday)",
        "  missing: Third MO   ~   80,000 lb  (~4% of a typical Monday)",
        "  missing: Fourth MO  ~   40,000 lb  (~2% of a typical Monday)",
        "  missing: Tiny MO    ~    1,600 lb  (~0% of a typical Monday)",
    ]


def test_a_thursday_index_date_is_judged_against_thursdays():
    """
    The weekday property through the whole path, not just expected(): a Monday
    barn is not on Thursday's roster, and the line says Thursday.

    MUTATION: the Monday anchor again. The Monday barn joins the roster, the
    Thursday barn leaves it, and the report names the wrong barn on a day it
    got right.
    """
    rows = (_full_weeks([(1, "Carthage")], index_date=THURSDAY)
            + _full_weeks([(2, "Joplin")], index_date=MONDAY))
    lines = br.report_lines(make_conn(rows, published="2026-09-16",
                                      extra_index_dates=[THURSDAY.isoformat()]))
    assert lines[0] == ("Barn report -- index date 2026-09-17 (Thu): "
                        "0 of 1 expected barns reported")
    assert len(lines) == 2, "Monday's barn is not on Thursday's roster"
    assert lines[1] == ("  missing: Carthage MO  ~80,000 lb  "
                        "(~100% of a typical Thursday)")


def test_missing_barns_rank_on_pounds_not_head():
    """
    MUTATION: sort on head, or sort ascending. The barn worth reading about
    stops being the first one printed, which on a long list is the whole
    difference between a useful line and a wall.
    """
    rows = (_full_weeks([(1, "Light")], head=100, wt=700.0)
            + _full_weeks([(2, "Heavy")], head=95, wt=850.0))
    lines = br.report_lines(make_conn(rows, published="2026-09-18",
                                      extra_index_dates=[MONDAY.isoformat()]))
    assert "Heavy" in lines[1] and "Light" in lines[2]
    assert "80,750 lb" in lines[1] and "70,000 lb" in lines[2]


def test_share_is_of_every_expected_barns_pounds():
    """
    The share is of what the whole weekday normally brings, so it shrinks as
    the rest of the day lands. One barn of 75,000 lb against a roster of
    100,000 lb reads 75%.

    MUTATION: divide by the missing barns' pounds alone (every line reads
    100%), or by the pounds that did report (a 4-barn Friday reads over 100%).
    """
    rows = (_full_weeks([(1, "Big")], head=100, wt=750.0)       # 75,000 lb
            + _full_weeks([(2, "Small")], head=100, wt=250.0))  # 25,000 lb
    rows += rows_for(2, "Small", [MONDAY.isoformat()], head=100, wt=250.0)
    lines = br.report_lines(make_conn(rows, published="2026-09-18"))
    assert "~75,000 lb" in lines[1]
    assert "(~75% of a typical Monday)" in lines[1]


def test_one_slug_reporting_does_not_cover_its_twin():
    """
    The same defect as the barn_days test above, seen from the output: Billings
    MT is two reports, and the one that sold must not answer for the one that
    did not.

    MUTATION: key presence on the printed name. The report goes quiet on
    exactly the morning it exists for, and says 2 of 2 reported.
    """
    rows = _full_weeks([(1774, "Billings"), (1777, "Billings")])
    rows += rows_for(1777, "Billings", [MONDAY.isoformat()])
    lines = br.report_lines(make_conn(rows, published="2026-09-18"))
    assert lines[0].endswith("1 of 2 expected barns reported")
    assert "missing: Billings" in lines[1]


def test_a_barns_name_is_its_first_spelling():
    """
    AMS respells a location mid-history -- "Joplin" becomes "Joplin Regional"
    -- while the slug_id stays put. The printed name must not follow, or the
    same barn reads as two different barns across a month of logs.

    The rows are seeded with the LATER spelling first, so insertion order alone
    picks the wrong one and all three mutations below have somewhere to fail.

    MUTATIONS: assign instead of setdefault (the last spelling wins); ORDER BY
    location DESC (the last spelling wins again); the ORDER BY dropped
    altogether, which on this fixture hands back insertion order -- verified,
    not assumed, because SQLite's DISTINCT sometimes sorts of its own accord and
    a mutation it happens to undo is not a mutation the test killed.
    """
    rows = (rows_for(1, "Joplin Regional", occurrences(range(7, 13)))
            + rows_for(1, "Joplin", occurrences(range(1, 7))))
    conn = make_conn(rows, published="2026-09-18",
                     extra_index_dates=[MONDAY.isoformat()])
    assert br._names(conn) == {1: "Joplin MO"}
    lines = br.report_lines(conn)
    assert "missing: Joplin MO" in lines[1] and "Regional" not in lines[1]


def test_a_day_with_no_sales_is_reported_not_skipped():
    """
    A holiday: the index has a value (its 7-day window is not empty) and not
    one barn sold. The report has to name that day, which is what the whole
    roster reading missing says -- no calendar required.

    MUTATION: offer only the dates sales exist for as `available`. The holiday
    is then not a candidate, the report quietly backs up to the previous
    business day, and the emptiest morning of the year prints as a normal one.
    """
    labor_day = date(2026, 9, 7)
    rows = _full_weeks([(1, "Carthage"), (2, "Tulsa")], index_date=labor_day)
    conn = make_conn(rows, published="2026-09-04",
                     extra_index_dates=[labor_day.isoformat()])
    lines = br.report_lines(conn)
    assert lines[0] == ("Barn report -- index date 2026-09-07 (Mon): "
                        "0 of 2 expected barns reported")


def test_the_index_date_is_cmes_publication_clock_not_the_newest_row():
    """
    CME published through Friday, so Monday is the date being estimated -- even
    though Tuesday already holds a row. See index_dates.py.

    MUTATION: MAX(report_date). The report answers about a day whose barns
    cannot have reported yet, every single morning.
    """
    rows = _full_weeks([(1, "Carthage")])
    rows += rows_for(1, "Carthage", ["2026-09-22"])
    lines = br.report_lines(make_conn(rows, published="2026-09-18",
                                      extra_index_dates=[MONDAY.isoformat()]))
    assert lines[0].startswith("Barn report -- index date 2026-09-21 (Mon):")


def test_the_index_date_is_cmes_newest_file_not_its_oldest():
    """
    Five CME files, not one. Every other fixture here gives cme_ftp_daily a
    single row, which leaves MAX(report_date) and MIN(report_date) the same
    value in all of them; the live table is years deep.

    MUTATION: MIN(report_date). CME's OLDEST file dates the report. Here that
    is Monday 09-14, whose next business day 09-15 is also a candidate, so the
    report quietly answers about the wrong day rather than failing loudly.
    """
    rows = _full_weeks([(1, "Carthage")])
    lines = br.report_lines(make_conn(
        rows,
        published=["2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17",
                   "2026-09-18"],
        extra_index_dates=["2026-09-15", MONDAY.isoformat()]))
    assert lines[0].startswith("Barn report -- index date 2026-09-21 (Mon):")


def test_an_empty_cme_table_is_not_a_published_date():
    """
    cme_ftp_daily PRESENT AND EMPTY -- a fresh backend, or a morning before the
    first pull ever ran -- is a different case from absent, and no fixture had
    it. SELECT MAX over no rows returns a row, holding None.

    MUTATION: `if row and row[0]` -> `if row`. The None reaches
    date.fromisoformat(), and the ValueError lands in the except that exists for
    a MISSING table -- so fci_daily never gets unioned into the candidates
    either, and the report backs up to the newest date a sale is held for. Here
    that is 09-14, where the barn did report: a morning with the whole roster
    absent prints as a complete day.
    """
    conn = make_conn(_full_weeks([(1, "Carthage")]),
                     extra_index_dates=[MONDAY.isoformat()])
    assert conn.execute("SELECT COUNT(*) FROM cme_ftp_daily").fetchone()[0] == 0
    lines = br.report_lines(conn)
    assert lines[0] == ("Barn report -- index date 2026-09-21 (Mon): "
                        "0 of 1 expected barns reported")


def test_the_index_date_falls_back_when_cmes_table_is_missing():
    """
    A backend without cme_ftp_daily must still get a report, on the newest date
    held.

    MUTATION: drop the try/except in _index_date(). The whole report collapses
    to "skipped" on that backend.
    """
    conn = make_conn(_full_weeks([(1, "Carthage")]))
    conn.execute("DROP TABLE cme_ftp_daily")
    lines = br.report_lines(conn)
    assert lines[0].startswith("Barn report -- index date 2026-09-14 (Mon):")


# ---------------------------------------------------------------------------
# The promise: a diagnostic must never strand a finished index.
# ---------------------------------------------------------------------------

def test_report_lines_cannot_raise():
    """
    MUTATION: remove the try/except. report_lines() runs after the index is
    computed and the snapshot frozen, and before the push -- a traceback here
    fails the run and strands a finished index unpublished.
    """
    empty = sqlite3.connect(":memory:")        # no mars_sales at all
    lines = br.report_lines(empty)
    assert lines and lines[0].startswith("Barn report skipped: OperationalError")


def test_report_lines_returns_a_materialised_list_of_strings(monkeypatch):
    """
    The caller iterates the return value. A generator, or a None, moves the
    failure into update_index.py where the guard used not to be.
    """
    monkeypatch.setattr(br, "expected", lambda *a: 1 / 0)
    lines = br.report_lines(make_conn(_full_weeks([(1, "Carthage")])))
    assert isinstance(lines, list) and all(isinstance(line, str) for line in lines)
    assert lines[0].startswith("Barn report skipped: ZeroDivisionError")


def test_no_sales_stored_at_all_is_one_plain_line():
    assert br.report_lines(make_conn()) == [
        "Barn report: no sales stored yet -- nothing to report."]


# ---------------------------------------------------------------------------
# The call site. The loop, not just the call, has to be inside a guard.
# ---------------------------------------------------------------------------

def _loop_is_guarded(src, call="barn_report.report_lines"):
    """True if every `for ... in <call>(...)` sits inside a try block."""
    tree = ast.parse(src)
    guarded, loops = set(), []
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            for child in ast.walk(node):
                guarded.add(id(child))
        if isinstance(node, ast.For) and call in ast.unparse(node.iter):
            loops.append(node)
    return bool(loops) and all(id(n) in guarded for n in loops)


def test_update_index_consumes_the_report_inside_a_guard():
    """
    MUTATION: unwrap the loop. Stubbing the report to return None then takes
    the whole run to exit 1 -- which is how this was found, outside the try,
    after nine database-level crash cases had been checked on the other side of
    the call.
    """
    assert _loop_is_guarded((REPO / "update_index.py").read_text(encoding="utf-8"))


def test_the_guard_check_can_actually_fail():
    """
    Guard the guard. This repo has shipped checks that could not fail -- one
    printed the same variable under both labels, one asserted a tautology, one
    banned a string in comments rather than in queries.
    """
    unguarded = ("for line in barn_report.report_lines(conn):\n"
                 "    print(line)\n")
    guarded = ("try:\n"
               "    for line in barn_report.report_lines(conn):\n"
               "        print(line)\n"
               "except Exception:\n"
               "    pass\n")
    assert not _loop_is_guarded(unguarded)
    assert _loop_is_guarded(guarded)
    assert not _loop_is_guarded("x = 1\n"), "no loop at all is not a pass"
