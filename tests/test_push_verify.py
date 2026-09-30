"""
The push's content check, proved against KNOWN-BAD input.

WHY THESE TESTS LOOK THE WAY THEY DO. The obvious test -- point the check at
the live pair and assert they agree -- is worthless. Everything agrees today,
so it passes forever whether the code works or not, and it is one careless
refactor away from feeding the same side in twice, which would make every push
look clean for the rest of time. CLAUDE.md names that failure exactly: "Three
checks written during this work could not [fail]... If you add a guard, prove
it fails on bad input before trusting it."

So every test here builds two SQLite fixtures, A and B, injects one specific
fault into B, and asserts on the REPORTED COLUMN AND BOTH VALUES -- not merely
that something failed. A check that says "mismatch" and a check that says
"fci_daily.FCI_VALUE scaled-sum: snowflake=10214946 sqlite=10215084" are not
the same tool at 08:00.

The expected aggregate values below are written out as literals, computed by
hand rather than by calling the code under test. A test whose expectation is
produced by the thing it is testing agrees with any bug.

A plays the SQLite side and B plays the Snowflake side. Both are really SQLite
here, which is what lets a fault be injected at all; the dialect difference is
covered separately by test_the_two_dialects_do_not_generate_the_same_sql.

Three tests assert the opposite -- that a fault is NOT caught, or that correct
data is NOT flagged. The first kind pins the one real gap (a permutation inside
a single column) so nobody has to rediscover it during an incident; the second
kind is what stops the check crying wolf on a good push, which would get it
switched off within a week. Both kinds pass trivially if the fixture never
actually differs, so each one first proves the two tables really are different
before believing anything the fingerprint says about them.
"""
import contextlib
import importlib.util
import io
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import push_verify as pv                                    # noqa: E402

DDL = """
CREATE TABLE fci_daily (
    report_date  TEXT,
    fci_value    REAL,
    n_locations  INTEGER,
    total_head   INTEGER,
    location     TEXT
)
"""

# Three real index days, with the 09-23 value from the incident report standing
# in for the first. Kept tiny so every expected aggregate can be worked out by
# hand and written down.
ROWS = [
    ("2026-09-11", 337.0638, 19, 17_250, "APACHE (South Central)"),
    ("2026-09-14", 341.7073, 21, 18_004, "BASSETT"),
    ("2026-09-15", 342.7373, 23, 19_110, "APACHE (South Central)"),
]

# Hand-computed, deliberately not derived from the code under test.
#   scaled:   3370638 + 3417073 + 3427373
SCALED_SUM = 10_215_084
TOTAL_HEAD_SUM = 54_364
N_LOC_SUM = 63
DATE_LEN_SUM = 30                       # 3 x len('2026-09-11')
LOC_LEN_SUM = 51                        # 22 + 7 + 22

# The Snowflake declared types, exactly as DESC TABLE reports them for the real
# schema. classify() reads these, never the column name.
SF_TYPES = {
    "REPORT_DATE": "DATE",
    "FCI_VALUE": "FLOAT",
    "N_LOCATIONS": "NUMBER(38,0)",
    "TOTAL_HEAD": "NUMBER(38,0)",
    "LOCATION": "VARCHAR(16777216)",
}

COLUMNS = [pv.Column(n, n.lower(), t, pv.classify(t)) for n, t in SF_TYPES.items()]


def build(path, rows=ROWS, ddl=DDL):
    conn = sqlite3.connect(str(path))
    conn.execute(ddl)
    n = len(conn.execute("PRAGMA table_info(fci_daily)").fetchall())
    conn.executemany(f"INSERT INTO fci_daily VALUES ({','.join('?' * n)})",
                     [r[:n] for r in rows])
    conn.commit()
    return conn


@pytest.fixture
def pair(tmp_path):
    """(A, B) -- two temp-FILE databases with identical contents."""
    a = build(tmp_path / "a.db")
    b = build(tmp_path / "b.db")
    yield a, b
    a.close()
    b.close()


def diff_for(a, b, column, aggregate, columns_b=None):
    """The one Difference for (column, aggregate), or None."""
    fa = pv.fingerprint(a, "fci_daily", COLUMNS, pv.SQLITE)
    fb = pv.fingerprint(b, "fci_daily", columns_b or COLUMNS, pv.SQLITE)
    for d in pv.compare(fa, fb, "fci_daily"):
        if d.column == column and d.aggregate == aggregate:
            return d
    return None


def all_diffs(a, b, columns_b=None):
    fa = pv.fingerprint(a, "fci_daily", COLUMNS, pv.SQLITE)
    fb = pv.fingerprint(b, "fci_daily", columns_b or COLUMNS, pv.SQLITE)
    return pv.compare(fa, fb, "fci_daily")


# ---------------------------------------------------------------------------
# The control. If this one ever fails the fault tests below mean nothing.
# ---------------------------------------------------------------------------

def test_identical_contents_produce_no_differences(pair):
    a, b = pair
    assert all_diffs(a, b) == []


def test_the_same_rows_in_a_different_order_compare_equal(tmp_path):
    """
    THE NEGATIVE DIRECTION, AND IT MATTERS AS MUCH AS THE FAULTS. write_pandas
    gives Snowflake no row order to preserve and micro-partitions are scanned in
    whatever order the warehouse likes, so if any aggregate here were
    order-sensitive the check would fail on correct data -- every run, forever --
    and the first thing anyone would do is turn it off.

    Reversed and rotated, because a symmetric fixture can pass a reversal by
    accident.
    """
    a = build(tmp_path / "order_a.db")
    rev = build(tmp_path / "order_rev.db", rows=list(reversed(ROWS)))
    rot = build(tmp_path / "order_rot.db", rows=[ROWS[2], ROWS[0], ROWS[1]])
    try:
        # Prove the stored order really differs; two tables written in the same
        # order would make the assertions below say nothing.
        scan = "SELECT report_date FROM fci_daily"          # no ORDER BY: insert order
        assert a.execute(scan).fetchall() != rev.execute(scan).fetchall()
        assert a.execute(scan).fetchall() != rot.execute(scan).fetchall()

        assert all_diffs(a, rev) == [], "the aggregates are order-sensitive"
        assert all_diffs(a, rot) == [], "the aggregates are order-sensitive"
    finally:
        a.close()
        rev.close()
        rot.close()


def test_the_fingerprint_is_the_values_we_think_it_is(pair):
    """
    Pin the actual aggregates, so a silent change to an expression -- someone
    "simplifying" the scaled sum, or dropping the prefix extremes -- fails here
    rather than quietly weakening every push from then on.
    """
    a, _ = pair
    fp = pv.fingerprint(a, "fci_daily", COLUMNS, pv.SQLITE)
    assert fp[(pv.ROWS, "rows")] == 3
    assert fp[("FCI_VALUE", "scaled-sum")] == SCALED_SUM
    assert fp[("FCI_VALUE", "count")] == 3
    assert fp[("TOTAL_HEAD", "sum")] == TOTAL_HEAD_SUM
    assert fp[("N_LOCATIONS", "sum")] == N_LOC_SUM
    assert fp[("REPORT_DATE", "len-sum")] == DATE_LEN_SUM
    assert fp[("REPORT_DATE", "distinct")] == 3
    assert fp[("REPORT_DATE", "min64")] == "2026-09-11"
    assert fp[("REPORT_DATE", "max64")] == "2026-09-15"
    assert fp[("LOCATION", "len-sum")] == LOC_LEN_SUM
    assert fp[("LOCATION", "distinct")] == 2
    assert fp[("LOCATION", "min64")] == "APACHE (South Central)"
    assert fp[("LOCATION", "max64")] == "BASSETT"


# ---------------------------------------------------------------------------
# One fault each, asserted on the reported column and BOTH values.
# ---------------------------------------------------------------------------

def test_a_value_off_by_one_ten_thousandth_is_caught(pair):
    """The finest difference the scale can see. 337.0638 -> 337.0639."""
    a, b = pair
    b.execute("UPDATE fci_daily SET fci_value = 337.0639 WHERE report_date = '2026-09-11'")
    d = diff_for(a, b, "FCI_VALUE", "scaled-sum")
    assert d is not None, "a 0.0001 change went undetected"
    assert (d.sqlite, d.snowflake) == (SCALED_SUM, SCALED_SUM + 1)
    assert "FCI_VALUE" in pv.describe([d])[0]


def test_the_incident_shape_is_caught(pair):
    """
    2026-09-29 for real: an index value served as 337.05 where it should have
    been 337.0638. 138 hundredths of a cent, on one row out of three.
    """
    a, b = pair
    b.execute("UPDATE fci_daily SET fci_value = 337.05 WHERE report_date = '2026-09-11'")
    d = diff_for(a, b, "FCI_VALUE", "scaled-sum")
    assert d is not None, "the 2026-09-29 corruption went undetected"
    assert (d.sqlite, d.snowflake) == (SCALED_SUM, SCALED_SUM - 138)
    line = pv.describe([d])[0]
    assert "fci_daily.FCI_VALUE" in line and "scaled-sum" in line
    assert str(SCALED_SUM) in line and str(SCALED_SUM - 138) in line


def test_a_deleted_row_is_caught(pair):
    a, b = pair
    b.execute("DELETE FROM fci_daily WHERE report_date = '2026-09-11'")
    rows = diff_for(a, b, pv.ROWS, "rows")
    assert rows is not None and (rows.sqlite, rows.snowflake) == (3, 2)
    scaled = diff_for(a, b, "FCI_VALUE", "scaled-sum")
    assert (scaled.sqlite, scaled.snowflake) == (SCALED_SUM, SCALED_SUM - 3_370_638)
    dates = diff_for(a, b, "REPORT_DATE", "len-sum")
    assert (dates.sqlite, dates.snowflake) == (DATE_LEN_SUM, DATE_LEN_SUM - 10)


def test_a_row_swapped_for_a_different_one_is_caught(pair, tmp_path):
    """
    ROW COUNT IDENTICAL, CONTENTS DIFFERENT: one index day dropped and another
    put in its place. This is the shape of a partial re-push -- a window
    recomputed against the wrong dates -- and COUNT(*) alone sees nothing at
    all.
    """
    a, _ = pair
    b = build(tmp_path / "swapped_row.db",
              rows=[ROWS[1], ROWS[2],
                    ("2026-09-21", 339.1111, 20, 17_000, "BASSETT")])
    try:
        assert diff_for(a, b, pv.ROWS, "rows") is None, (
            "the fixture must keep the row count identical or it proves nothing "
            "beyond what the old COUNT check already did")
        caught = {(d.column, d.aggregate) for d in all_diffs(a, b)}
        assert ("FCI_VALUE", "scaled-sum") in caught
        assert ("REPORT_DATE", "min64") in caught and ("REPORT_DATE", "max64") in caught
        assert ("TOTAL_HEAD", "sum") in caught

        scaled = diff_for(a, b, "FCI_VALUE", "scaled-sum")
        assert (scaled.sqlite, scaled.snowflake) == (SCALED_SUM, 10_235_557)
        lo = diff_for(a, b, "REPORT_DATE", "min64")
        assert (lo.sqlite, lo.snowflake) == ("2026-09-11", "2026-09-14")
    finally:
        b.close()


def test_a_nulled_value_is_caught_by_the_per_column_count(pair):
    """
    NULL-vs-value is exactly what a table-level COUNT(*) cannot see: the row is
    still there. COUNT(col) is why every column carries one.
    """
    a, b = pair
    b.execute("UPDATE fci_daily SET fci_value = NULL WHERE report_date = '2026-09-11'")
    assert diff_for(a, b, pv.ROWS, "rows") is None, "the row count should be unchanged"
    cnt = diff_for(a, b, "FCI_VALUE", "count")
    assert cnt is not None and (cnt.sqlite, cnt.snowflake) == (3, 2)


def test_a_null_turned_into_a_zero_is_caught_by_the_same_count(tmp_path):
    """
    The other direction, and the more dangerous one: a missing price arriving
    as 0.0 rather than as NULL.

    Nothing else in the fingerprint can see it. The row count is unchanged, and
    a zero adds nothing to the scaled sum -- so this is the case that earns
    COUNT(col) its place on every column, and both of those are asserted rather
    than just the catch. "Zero is not a price" is already a documented trap in
    this pipeline (37% of Direct Hay rows carry a literal 0); a backend that
    silently coerced NULL to 0 on the way in would show up here and nowhere
    else.
    """
    a = build(tmp_path / "null_a.db", rows=ROWS + [("2026-09-16", None, 0, 0, "X")])
    b = build(tmp_path / "null_b.db", rows=ROWS + [("2026-09-16", 0.0, 0, 0, "X")])
    try:
        assert diff_for(a, b, pv.ROWS, "rows") is None, "the row count must be identical"
        assert diff_for(a, b, "FCI_VALUE", "scaled-sum") is None, (
            "a zero contributes nothing to the sum -- if this fixture ever makes "
            "the sum differ it stops testing what it claims to")
        cnt = diff_for(a, b, "FCI_VALUE", "count")
        assert cnt is not None, "NULL -> 0 went undetected; COUNT(col) is the only catcher"
        assert (cnt.sqlite, cnt.snowflake) == (3, 4)
        assert "FCI_VALUE" in pv.describe([cnt])[0]
    finally:
        a.close()
        b.close()


def test_a_length_preserving_text_substitution_is_caught(pair):
    """
    THE HOLE THAT MOTIVATED THE PREFIX EXTREMES. A negative control against the
    live data rewrote every 'a' to 'x' in mars_sales.location; both sides stayed
    at (99 distinct, 284587 chars) and the check said SAME. Here the same shape:
    'APACHE...' -> 'XPACHE...' changes neither total length nor cardinality.
    """
    a, b = pair
    b.execute("UPDATE fci_daily SET location = 'XPACHE (South Central)' "
              "WHERE location = 'APACHE (South Central)'")
    assert diff_for(a, b, "LOCATION", "len-sum") is None
    assert diff_for(a, b, "LOCATION", "distinct") is None
    lo = diff_for(a, b, "LOCATION", "min64")
    hi = diff_for(a, b, "LOCATION", "max64")
    assert lo is not None and hi is not None, (
        "a length- and cardinality-preserving substitution slipped through; "
        "the 64-character MIN/MAX prefix is what catches it")
    assert (lo.sqlite, lo.snowflake) == ("APACHE (South Central)", "BASSETT")
    assert (hi.sqlite, hi.snowflake) == ("BASSETT", "XPACHE (South Central)")


def test_sum_length_and_distinct_alone_would_have_missed_it(pair):
    """
    Guard the guard, from the other side: prove the prefix extremes are doing
    real work rather than being decoration nobody would miss. With only the two
    aggregates the original design specified, the substitution above is
    invisible.
    """
    a, b = pair
    b.execute("UPDATE fci_daily SET location = 'XPACHE (South Central)' "
              "WHERE location = 'APACHE (South Central)'")
    weaker = [d for d in all_diffs(a, b) if d.aggregate in ("len-sum", "distinct", "count")]
    assert weaker == [], (
        "this fixture no longer demonstrates the blind spot, so the test above "
        "proves nothing about MIN/MAX -- fix the fixture, not this assertion")


def test_a_dropped_column_is_caught_and_named(pair, tmp_path):
    """
    A column that vanishes from one side must be reported, not silently fall
    out of the intersection. The real push already drops mars_sales.
    published_date on purpose; an UNEXPECTED drop is what this catches.
    """
    a, _ = pair
    short_ddl = DDL.replace("    total_head   INTEGER,\n    location     TEXT\n",
                            "    total_head   INTEGER\n")
    assert "location     TEXT" not in short_ddl, "the fixture still has the column"
    b = build(tmp_path / "short.db",
              rows=[r[:4] for r in ROWS], ddl=short_ddl)
    cols_b = [c for c in COLUMNS if c.name != "LOCATION"]
    try:
        diffs = {(d.column, d.aggregate): d for d in all_diffs(a, b, columns_b=cols_b)}
        assert ("LOCATION", "len-sum") in diffs, "a dropped column was not reported"
        d = diffs[("LOCATION", "len-sum")]
        assert (d.sqlite, d.snowflake) == (LOC_LEN_SUM, "<absent>")
        assert "LOCATION" in pv.describe([d])[0]
    finally:
        b.close()


# ---------------------------------------------------------------------------
# THE GAP. A permutation inside one column is INVISIBLE to this check, and
# these tests pin that rather than hide it. Read the block comment below before
# concluding the fingerprint is weaker than it should be -- it is a property of
# aggregates, not an oversight, and the tests here measure what closing it
# would cost.
# ---------------------------------------------------------------------------

def swap_two_fci_values(conn):
    """09-11's value and 09-14's value change places. Multiset untouched."""
    conn.execute("UPDATE fci_daily SET fci_value = 341.7073 WHERE report_date = '2026-09-11'")
    conn.execute("UPDATE fci_daily SET fci_value = 337.0638 WHERE report_date = '2026-09-14'")


def test_two_rows_with_their_values_swapped_are_NOT_caught(pair):
    """
    NOT CAUGHT. Stated plainly because a documented gap is worth more than a
    claim of coverage, and because the next person to read this file will
    otherwise assume the swap is covered.

    Two index days exchange their values: 2026-09-11 serves 341.7073 and
    2026-09-14 serves 337.0638. Row count identical, non-null count identical,
    scaled sum identical -- every aggregate in the fingerprint agrees, and the
    push prints [CONTENT OK] over two wrong published numbers.

    WHY NO SINGLE-COLUMN AGGREGATE CAN EVER CATCH THIS. An aggregate over one
    column reads that column's multiset of values and nothing else. A swap
    permutes the multiset into itself. So SUM, COUNT, COUNT(DISTINCT), MIN, MAX
    and every higher moment are identical BY CONSTRUCTION, not by bad luck --
    which is why COUNT(DISTINCT), suggested as the fix, cannot be one. Asserted
    below rather than argued: the two aggregates the brief proposed, plus a sum
    of squares, all agree on the corrupted table.

    What this costs in practice is smaller than it looks. The corruption has to
    move values BETWEEN ROWS OF ONE COLUMN while leaving every other column
    where it was. A stale load, a partial load, a lost row, a shifted column
    read into the wrong field, a re-run against the wrong window -- all of those
    change a count or a sum and all are caught. The realistic member of this
    family is a whole column rotated by one row, which is the test below.
    """
    a, b = pair
    swap_two_fci_values(b)

    # A test that asserts "nothing was caught" passes perfectly if the fixture
    # corrupted nothing, which is the tautology CLAUDE.md warns about. So prove
    # the two tables really do differ row by row, and really are a permutation
    # of each other, before believing anything the fingerprint says about them.
    seen = "SELECT report_date, fci_value FROM fci_daily ORDER BY report_date"
    rows_a, rows_b = a.execute(seen).fetchall(), b.execute(seen).fetchall()
    assert rows_a != rows_b, "the fixture swapped nothing, so this proves nothing"
    assert sorted(v for _, v in rows_a) == sorted(v for _, v in rows_b), (
        "the fixture changed the multiset, so it is not testing a permutation")

    assert all_diffs(a, b) == [], (
        "the swap is now caught -- good, but this test documents a gap that no "
        "longer exists, so REWRITE IT to assert the catch instead of deleting it")

    # The two remedies the brief asked about, and one stronger than either.
    sql = {
        "distinct": "SELECT COUNT(DISTINCT fci_value) FROM fci_daily",
        "min/max": "SELECT MIN(fci_value), MAX(fci_value) FROM fci_daily",
        "sum of squares": "SELECT SUM(fci_value * fci_value) FROM fci_daily",
    }
    for name, q in sql.items():
        assert a.execute(q).fetchone() == b.execute(q).fetchone(), (
            f"{name} distinguishes a permutation, which is impossible for a "
            f"single-column aggregate -- check the fixture actually swapped")


def test_a_column_shifted_by_one_row_is_not_caught_either(pair, tmp_path):
    """
    The realistic form of the gap, and the reason it is worth writing down: an
    off-by-one during a load rotates one column against the others. Every value
    in the table is still present exactly once, so the multiset -- and every
    aggregate over it -- is unchanged, while every row is wrong.

    total_head rotated one row down: 17250/18004/19110 becomes
    19110/17250/18004, sum 54364 either way.
    """
    a, _ = pair
    rotated = [
        (ROWS[0][0], ROWS[0][1], ROWS[0][2], ROWS[2][3], ROWS[0][4]),
        (ROWS[1][0], ROWS[1][1], ROWS[1][2], ROWS[0][3], ROWS[1][4]),
        (ROWS[2][0], ROWS[2][1], ROWS[2][2], ROWS[1][3], ROWS[2][4]),
    ]
    assert sum(r[3] for r in rotated) == TOTAL_HEAD_SUM, "the fixture must be a rotation"
    assert [r[3] for r in rotated] != [r[3] for r in ROWS], "nothing was rotated"
    b = build(tmp_path / "rotated.db", rows=rotated)
    try:
        seen = "SELECT report_date, total_head FROM fci_daily ORDER BY report_date"
        assert a.execute(seen).fetchall() != b.execute(seen).fetchall(), (
            "the fixture is not actually corrupted, so this proves nothing")
        assert all_diffs(a, b) == [], (
            "a rotated column is now caught -- rewrite this test to assert the "
            "catch rather than removing it")
    finally:
        b.close()


def test_only_a_cross_column_term_closes_the_gap(pair):
    """
    What WOULD work, so the gap above is a measured trade and not a shrug.

    Tying each value to something else in its own row breaks the permutation
    symmetry: SUM(scaled(fci_value) * total_head) changes as soon as two rows
    exchange values, as long as their partners differ. It stays a single
    server-side aggregate, so it costs nothing extra in round trips, and it
    needs no per-table configuration -- the partner can be the next numeric
    column in DESC TABLE order.

    It is not adopted, for the reason the next test measures.
    """
    a, b = pair
    swap_two_fci_values(b)
    paired = ("SELECT SUM(CAST(ROUND(fci_value * 10000) AS INTEGER) * total_head) "
              "FROM fci_daily")
    before = a.execute(paired).fetchone()[0]
    after = b.execute(paired).fetchone()[0]
    assert before != after, (
        "a cross-column term missed the swap too, which would mean the gap is "
        "not closable this way after all")
    assert (before, after) == (185_161_585_822, 185_126_573_832)


def test_the_cross_column_remedy_does_not_fit_in_int64_on_the_real_data():
    """
    WHY THE GAP STAYS OPEN. The remedy above multiplies two scaled columns
    together, and on the REAL database that product does not fit.

    Measured read-only across all 20 tables, pairing each numeric column with
    the next one in schema order: replacement_sales.avg_weight x avg_price
    reaches 7.75e18, which is 84% of the int64 limit, and avg_price and
    price_min overflow outright -- SQLite raises. Snowflake would NOT overflow
    at the same point, because it sums into NUMBER(38,0); the check would fail
    on the local side of a perfectly good push and keep failing. A guard that
    cries wolf is worse than the gap it closes, and this codebase has already
    written that lesson down twice.

    The only variant that fits is a modular one -- (scaled % p) * (partner % p),
    which measures 3.26e16 at its worst, 283x inside the limit. It is NOT built
    here: it needs MOD's sign semantics verified live on both backends first
    (cme_ftp_daily.reported_change carries 1,240 negative rows), and two tables
    have fewer than two numeric columns and would get no coverage from it at
    all. Written down so the option is costed rather than forgotten.
    """
    db = REPO / "data" / "mars_history.db"
    if not db.exists():
        pytest.skip("no local database")
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    limit = 2 ** 63 - 1
    unsafe, worst = [], ("", 0)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        for t in tables:
            info = list(conn.execute(f"PRAGMA table_info({t})"))
            types = {r[1]: (r[2] or "").upper() for r in info}
            nums = [r[1] for r in info if types[r[1]].startswith(("REAL", "INT"))]
            if len(nums) < 2:
                continue

            def scaled(c):
                return (f'CAST(ROUND("{c}" * {pv.FLOAT_SCALE}) AS INTEGER)'
                        if types[c].startswith("REAL") else f'"{c}"')

            for i, col in enumerate(nums):
                partner = nums[(i + 1) % len(nums)]
                q = (f"SELECT SUM(ABS({scaled(col)} * {scaled(partner)})) "
                     f'FROM "{t}"')
                try:
                    v = conn.execute(q).fetchone()[0] or 0
                except sqlite3.OperationalError:
                    unsafe.append(f"{t}.{col} x {partner} (overflows)")
                    continue
                if v > worst[1]:
                    worst = (f"{t}.{col} x {partner}", v)
                if v > limit // 10:
                    unsafe.append(f"{t}.{col} x {partner} = {v:.3g}")
    finally:
        conn.close()

    assert unsafe, (
        f"the cross-column term now fits everywhere (worst {worst[0]} at "
        f"{worst[1]:.3g}, {limit / max(worst[1], 1):.0f}x inside int64), so the "
        f"reason for leaving the permutation gap open no longer holds. "
        f"Re-evaluate adopting it -- do not just delete this test.")


# ---------------------------------------------------------------------------
# The same-side-twice mistake, and the arithmetic that makes the two sides
# comparable at all.
# ---------------------------------------------------------------------------

def test_the_two_dialects_do_not_generate_the_same_sql():
    """
    STRUCTURAL DEFENCE AGAINST FEEDING ONE SIDE IN TWICE. If a refactor ever
    made both fingerprints run the same statement against the same connection,
    every push would look clean forever and nothing else here would notice.
    The two dialects differ in identifier quoting, in the integer cast, and in
    how a DATE is rendered -- assert all three, so an accidental collapse
    cannot be papered over by one of them still differing.
    """
    lite, lite_labels = pv.fingerprint_sql("fci_daily", COLUMNS, pv.SQLITE)
    snow, snow_labels = pv.fingerprint_sql("fci_daily", COLUMNS, pv.SNOWFLAKE)
    assert lite != snow
    assert lite_labels == snow_labels, "the two sides must line up label for label"
    assert '"fci_value"' in lite and '"fci_value"' not in snow
    assert "AS INTEGER" in lite and "AS BIGINT" in snow
    assert "TO_CHAR" in snow and "TO_CHAR" not in lite

    with pytest.raises(ValueError):
        pv.fingerprint_sql("fci_daily", COLUMNS, "postgres")


def test_the_outer_round_is_load_bearing():
    """
    SQLite's CAST truncates toward zero; Snowflake's rounds half-away-from-zero
    (verified live: CAST(2.9 AS BIGINT) is 3 there, CAST(2.9 AS INTEGER) is 2
    here; CAST(3370637.9999999995 AS ...) is 3370638 against 3370637). Without
    the outer ROUND the two backends disagree on nearly every float column,
    forever, on correct data.

    So: the expression must contain ROUND, and the arithmetic must actually
    need it.
    """
    assert "ROUND" in pv._int_cast("x * 10000", pv.SQLITE)
    assert "ROUND" in pv._int_cast("x * 10000", pv.SNOWFLAKE)

    c = sqlite3.connect(":memory:")
    try:
        naive = c.execute("SELECT CAST(3370637.9999999995 AS INTEGER), "
                          "CAST(2.9 AS INTEGER), CAST(-2.9 AS INTEGER)").fetchone()
        assert naive == (3370637, 2, -2), (
            "SQLite no longer truncates, so the comment explaining why ROUND is "
            "here is out of date -- re-verify against Snowflake before relying "
            "on it")
        rounded = c.execute("SELECT CAST(ROUND(3370637.9999999995) AS INTEGER), "
                            "CAST(ROUND(2.9) AS INTEGER), CAST(ROUND(-2.9) AS INTEGER)"
                            ).fetchone()
        assert rounded == (3370638, 3, -3), (
            "with ROUND both backends agree; this is the whole reason it is there")
    finally:
        c.close()


def test_rounding_to_four_decimals_then_scaling_is_not_a_substitute(pair):
    """
    ROUND(col, 4) * 10000 looks equivalent and is not: on SQLite it loses units
    (fci_value's real scaled sum came out 2966923663 against the correct
    2966923734), while Snowflake gets it right -- so adopting it would look
    like Snowflake-side corruption on every single run. Scale first, round once.
    """
    a, _ = pair
    wrong = a.execute(
        "SELECT SUM(CAST(ROUND(fci_value, 4) * 10000 AS INTEGER)) FROM fci_daily"
    ).fetchone()[0]
    right = pv.fingerprint(a, "fci_daily", COLUMNS, pv.SQLITE)[("FCI_VALUE", "scaled-sum")]
    assert right == SCALED_SUM
    assert wrong != right, (
        "this fixture no longer demonstrates the difference, so it no longer "
        "protects the expression -- pick values that do")


# ---------------------------------------------------------------------------
# Classification, which is what makes the check schema-driven.
# ---------------------------------------------------------------------------

def test_classify_covers_every_type_pair_in_the_live_schema():
    """The five pairs that exist across all 20 pushed tables, plus the shapes
    that do not exist yet but would arrive classified correctly."""
    assert pv.classify("DATE") == "date"
    assert pv.classify("VARCHAR(16777216)") == "text"
    assert pv.classify("NUMBER(38,0)") == "int"
    assert pv.classify("FLOAT") == "float"
    # Not in the schema today:
    assert pv.classify("NUMBER(12,4)") == "float", "a scaled NUMBER must not be summed raw"
    assert pv.classify("TIMESTAMP_NTZ(9)") == "text"
    assert pv.classify("BOOLEAN") == "text"
    assert pv.classify("VARIANT") == "other"
    assert pv.classify("") == "other"


def test_a_column_is_classified_by_its_type_and_never_by_its_name():
    """
    mars_census.report_date/.raw_date/.index_date are VARCHAR on BOTH sides
    while fci_daily.report_date is TEXT -> DATE. A name-based date rule picks
    the wrong expression for three real columns on day one.
    """
    looks_like_a_date = pv.Column("REPORT_DATE", "report_date",
                                  "VARCHAR(16777216)", pv.classify("VARCHAR(16777216)"))
    assert looks_like_a_date.cls == "text"
    sql = dict(pv.column_aggregates(looks_like_a_date, pv.SNOWFLAKE))
    assert "TO_CHAR" not in sql["len-sum"], (
        "a VARCHAR column named report_date was given the DATE expression")

    really_a_date = pv.Column("REPORT_DATE", "report_date", "DATE", pv.classify("DATE"))
    assert "TO_CHAR" in dict(pv.column_aggregates(really_a_date, pv.SNOWFLAKE))["len-sum"]


def test_every_column_gets_a_count_even_when_unfingerprintable():
    """
    An unknown type must degrade to "row presence checked, values not" with a
    warning, never to nothing. The exclusion is by TYPE, from DESC TABLE --
    there is no name list anywhere that a new column could be quietly added to.
    """
    weird = pv.Column("PAYLOAD", "payload", "VARIANT", pv.classify("VARIANT"))
    aggs = dict(pv.column_aggregates(weird, pv.SNOWFLAKE))
    assert list(aggs) == ["count"]
    assert aggs["count"] == "COUNT(PAYLOAD)"

    for col in COLUMNS:
        assert "count" in dict(pv.column_aggregates(col, pv.SQLITE)), col.name


# ---------------------------------------------------------------------------
# The schema intersection and the end-to-end verdict, against a stand-in for
# the Snowflake connection.
# ---------------------------------------------------------------------------

# Snowflake format model -> strftime. Longest-first where one is a prefix of
# another, so YYYY is consumed before YY.
_TO_CHAR_TOKENS = (("YYYY", "%Y"), ("YY", "%y"), ("HH24", "%H"),
                   ("MM", "%m"), ("MI", "%M"), ("DD", "%d"), ("SS", "%S"))


def sf_to_char(value, fmt):
    """
    Snowflake's TO_CHAR(<DATE>, <format>), modelled faithfully enough to tell
    one format from another.

    THE STAND-IN USED TO BE `lambda v, fmt: v`, WHICH IGNORED THE FORMAT
    ENTIRELY. That is exactly why changing _text_expr's pinned 'YYYY-MM-DD' to
    'DD-MM-YYYY' passed every test in this file: the only assertion anywhere
    was that the substring "TO_CHAR" appeared, and the stub rendered every
    format the same way. A stub that cannot distinguish two formats cannot
    defend the one that makes the two backends comparable.

    An unrecognised format raises rather than falling back to the identity --
    a silent fallback is how the hole got here in the first place.
    """
    if value is None:
        return None
    leftover = fmt
    for token, _code in _TO_CHAR_TOKENS:
        leftover = leftover.replace(token, "")
    if any(ch.isalnum() for ch in leftover):
        raise ValueError(f"TO_CHAR format {fmt!r} is not modelled by this stub")
    pattern = fmt
    for token, code in _TO_CHAR_TOKENS:
        pattern = pattern.replace(token, code)
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").strftime(pattern)


class FakeSnowflake:
    """
    A SQLite connection wearing Snowflake's clothes: it answers DESC TABLE and
    understands TO_CHAR. Enough to drive pushed_columns() and verify_table()
    end to end, including the DATE branch that a pure SQLite pair would skip.
    """

    def __init__(self, conn, types=None, boom=None):
        self.conn = conn
        self.types = types or SF_TYPES
        self.boom = boom
        conn.create_function("TO_CHAR", 2, sf_to_char)

    class _Cur:
        def __init__(self, outer):
            self.outer = outer
            self.rows = None

        def execute(self, sql):
            if self.outer.boom:
                raise self.outer.boom
            if sql.upper().startswith("DESC TABLE"):
                self.rows = [(n, t) for n, t in self.outer.types.items()]
                return self.rows
            self.rows = self.outer.conn.execute(sql).fetchall()
            return self.rows

        def fetchone(self):
            return self.rows[0] if self.rows else None

        def __iter__(self):
            return iter(self.rows)

    def cursor(self):
        return self._Cur(self)


def test_pushed_columns_intersects_the_two_schemas_and_reports_the_drop(pair):
    """
    The column set must be derived the way the push derives it, so the check is
    never fingerprinting a different set than the push wrote. A SQLite-only
    column (the real one is mars_sales.published_date) is dropped AND named.
    """
    a, b = pair
    a.execute("ALTER TABLE fci_daily ADD COLUMN published_date TEXT")
    cols, dropped = pv.pushed_columns(a, FakeSnowflake(b), "fci_daily")
    assert [c.name for c in cols] == list(SF_TYPES)
    assert dropped == ["PUBLISHED_DATE"]
    assert [c.cls for c in cols] == ["date", "float", "int", "int", "text"]


def test_verify_table_end_to_end_ok_and_mismatch(pair):
    a, b = pair
    v = pv.verify_table(a, FakeSnowflake(b), "fci_daily")
    assert v.status == "OK" and v.verified and v.rows == 3
    assert v.differences == [] and v.unclassified == []
    assert "CONTENT OK" in v.summary()

    b.execute("UPDATE fci_daily SET fci_value = 337.05 WHERE report_date = '2026-09-11'")
    v = pv.verify_table(a, FakeSnowflake(b), "fci_daily")
    assert v.status == "MISMATCH" and not v.verified
    assert "CONTENT MISMATCH" in v.summary()
    d, = [x for x in v.differences if x.aggregate == "scaled-sum"]
    assert (d.sqlite, d.snowflake) == (SCALED_SUM, SCALED_SUM - 138)


def test_a_broken_check_is_inconclusive_and_never_raises(pair):
    """
    The index is already COMMITTED when this runs. A Snowflake hiccup here must
    not turn a successful push into a failure -- but it must also not look like
    a pass, or the guard silently decays back into the count check it replaced.
    """
    a, b = pair
    v = pv.verify_table(a, FakeSnowflake(b, boom=RuntimeError("connection reset")),
                        "fci_daily")
    assert v.status == "INCONCLUSIVE" and not v.verified
    assert "INCONCLUSIVE" in v.summary() and "connection reset" in v.summary()
    assert "CONTENT OK" not in v.summary(), (
        "inconclusive and verified must never print the same way")


def test_an_empty_table_is_reported_as_empty_and_not_as_a_pass(tmp_path):
    """
    mars_census is 0 rows on both sides, so all 32 of its aggregates are
    NULL == NULL. That agreement proves nothing and must never be read as
    coverage.
    """
    a = build(tmp_path / "ea.db", rows=[])
    b = build(tmp_path / "eb.db", rows=[])
    try:
        v = pv.verify_table(a, FakeSnowflake(b), "fci_daily")
        assert v.status == "EMPTY"
        assert v.differences == []
        assert not v.verified, "an empty table must not count as verified"
        assert "nothing was verified" in v.summary()
    finally:
        a.close()
        b.close()


ALIEN_DDL = """
CREATE TABLE fci_daily (
    dt TEXT, val REAL, nloc INTEGER, head INTEGER, loc TEXT
)
"""

# The same five classes under five names the local side does not have.
ALIEN_TYPES = {"DT": "DATE", "VAL": "FLOAT", "NLOC": "NUMBER(38,0)",
               "HEAD": "NUMBER(38,0)", "LOC": "VARCHAR(16777216)"}


def test_a_table_whose_schemas_share_no_column_is_not_a_pass(tmp_path):
    """
    NO COLUMNS HAD NO TEST AT ALL, and `status = "NO COLUMNS"` -> `"OK"` was a
    one-line edit that left the whole suite green.

    It is the worst of the non-mismatch verdicts to get wrong. Rows exist on
    both sides and their counts agree, so the cheap check prints [OK]; the two
    schemas share no column, so the ONLY thing the fingerprint compared was
    COUNT(*) -- which is the exact check that missed the 2026-09-29 incident.
    Reporting that as OK/verified restores the blind spot under a label that
    says it was checked.

    The Snowflake-only columns are all NULL here, which is what the push
    itself leaves behind: it DELETEs every row and reloads from a DataFrame
    carrying only the intersection, so a non-zero count there would mean rows
    came from somewhere else and is reported separately.
    """
    a = build(tmp_path / "na.db")
    b = build(tmp_path / "nb.db", rows=[(None,) * 5] * 3, ddl=ALIEN_DDL)
    try:
        v = pv.verify_table(a, FakeSnowflake(b, types=ALIEN_TYPES), "fci_daily")
        assert v.rows == 3, "both sides must really have rows, or this is EMPTY"
        assert v.differences == [], (
            "the counts must agree, or this is caught as a MISMATCH and says "
            "nothing about the NO COLUMNS path")
        assert v.status == "NO COLUMNS", (
            f"a table with no shared column reported {v.status!r}; only "
            f"COUNT(*) was compared and every value could be wrong")
        assert not v.verified, "nothing was verified, so this is not a pass"
        assert "only COUNT(*) was compared" in v.summary()
        assert "CONTENT OK" not in v.summary()
        assert sorted(v.snowflake_only) == sorted(ALIEN_TYPES), (
            "the columns that exist only in Snowflake must be named")
    finally:
        a.close()
        b.close()


def test_an_unknown_type_is_named_out_loud(pair):
    a, b = pair
    types = dict(SF_TYPES, LOCATION="GEOGRAPHY")
    v = pv.verify_table(a, FakeSnowflake(b, types=types), "fci_daily")
    assert v.unclassified == ["LOCATION"]
    assert v.status == "OK"     # still checked for row presence


# ---------------------------------------------------------------------------
# Reporting, and the arithmetic headroom, against the real database.
# ---------------------------------------------------------------------------

def test_describe_names_the_table_the_column_and_both_values():
    d = pv.Difference("fci_daily", "FCI_VALUE", "scaled-sum", 2966923536, 2966923734)
    line, = pv.describe([d])
    for token in ("fci_daily", "FCI_VALUE", "scaled-sum", "2966923536", "2966923734"):
        assert token in line, f"{token!r} missing from {line!r}"
    assert "-198" in line, "the direction and size of the drift belong in the line"

    rows, = pv.describe([pv.Difference("fci_daily", pv.ROWS, "rows", 953, 954)])
    assert "fci_daily row count" in rows

    text, = pv.describe([pv.Difference("m", "LOC", "min64", "BASSETT", "APACHE")])
    assert "BASSETT" in text and "APACHE" in text


# ---------------------------------------------------------------------------
# THE HELPER IS NOT THE GUARD; THE CALL IS. Everything above could be perfect
# with verify_table never invoked, and every test here would stay green while
# the push went on printing [OK] on a corrupted table. Read off the source so
# a mention in a comment cannot satisfy it.
# ---------------------------------------------------------------------------

MIGRATE = REPO / "snowflake" / "02_migrate_data.py"
DAILY = REPO / "scripts" / "daily_update.ps1"


def _push_loop():
    import ast
    tree = ast.parse(MIGRATE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.For) and ast.unparse(node.target) == "table":
            return node, tree
    raise AssertionError("02_migrate_data.py no longer has a per-table loop")


def test_the_push_calls_the_content_check_and_calls_it_after_the_commit():
    """
    Before the COMMIT the fingerprint would be measuring an uncommitted state
    nobody will ever read; after it, the DELETE/INSERT has already invalidated
    Snowflake's result cache so there is no stale-read risk either.
    """
    import ast
    loop, _ = _push_loop()
    calls = {ast.unparse(n.func): n.lineno for n in ast.walk(loop)
             if isinstance(n, ast.Call)}
    assert "push_verify.verify_table" in calls, (
        "the push loop no longer runs the content check; it is back to a row "
        "count, which is blind to every value in the table")
    # The statement, not the word: a print() that mentions COMMITTED is not a
    # commit, and matching on the word made this assert on the wrong line.
    commits = [n.lineno for n in ast.walk(loop) if isinstance(n, ast.Call)
               and ast.unparse(n).replace('"', "'") == "cur.execute('COMMIT')"]
    assert commits and calls["push_verify.verify_table"] > max(commits), (
        "the content check runs before the COMMIT, so it is fingerprinting a "
        "state that is not what Snowflake will serve")


def test_a_content_failure_exits_3_and_not_the_generic_failure_code():
    """
    Exit 3 exists because daily_update.ps1's generic message says Snowflake
    "still holds its previous contents" -- true after a rollback, false here.
    """
    import ast
    _, tree = _push_loop()
    codes = {ast.unparse(n.exc) for n in ast.walk(tree) if isinstance(n, ast.Raise)
             and n.exc is not None and "SystemExit" in ast.unparse(n.exc)}
    assert "SystemExit(3)" in codes, (
        f"no SystemExit(3) in the push; found {sorted(codes)}. A content "
        f"failure would exit 0 or share the rollback code.")

    src = MIGRATE.read_text(encoding="utf-8")
    assert "critical_content" in src and "optional_content" in src, (
        "the CRITICAL/OPTIONAL split must apply to content failures too: a "
        "dashboard table drifting is not an index failure, and the reverse "
        "must not be silent")


def test_the_daily_job_says_something_true_about_exit_3():
    """
    The subtle one. Reusing the existing branch would print "Snowflake still
    holds its previous contents" at the exact moment somebody is trying to
    understand a real incident -- and the write COMMITTED, so it does not.
    """
    if not DAILY.exists():
        pytest.skip("daily_update.ps1 not present")
    src = DAILY.read_text(encoding="utf-8")
    assert "$pushCode -eq 3" in src, (
        "daily_update.ps1 has no branch for exit 3, so a content failure would "
        "be logged as a stale dashboard")
    branch = src.split("$pushCode -eq 3", 1)[1].split("} else {", 1)[0]
    assert "still holds its previous contents" not in branch, (
        "the exit-3 branch repeats the rollback sentence, which is false here")
    lower = branch.lower()
    assert "committed" in lower and "wrong values" in lower, (
        "the exit-3 message must say the write committed and the dashboard may "
        "be serving wrong values")


def test_the_scaled_sums_stay_far_inside_int64():
    """
    Against the REAL database, read-only. The scaling only works because an
    int64 holds it exactly on both sides; the worst column measured was 8.79e11,
    about 1e-7 of the limit. This fails long before an overflow could turn into
    a silent wrong answer.
    """
    db = REPO / "data" / "mars_history.db"
    if not db.exists():
        pytest.skip("no local database")
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    limit = 2 ** 63 - 1
    worst = ("", 0)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        for t in tables:
            reals = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")
                     if (r[2] or "").upper().startswith("REAL")]
            for c in reals:
                v = conn.execute(
                    f'SELECT SUM(ABS(CAST(ROUND("{c}" * {pv.FLOAT_SCALE}) AS INTEGER))) '
                    f"FROM {t}").fetchone()[0] or 0
                if v > worst[1]:
                    worst = (f"{t}.{c}", v)
    finally:
        conn.close()
    assert worst[1] < limit // 1000, (
        f"{worst[0]} scales to {worst[1]}, within 1000x of the int64 limit. "
        f"Reduce FLOAT_SCALE before a sum silently wraps.")


# ---------------------------------------------------------------------------
# THE WIRING, DRIVEN FOR REAL.
#
# Everything above this line tests push_verify.py. The block below runs
# snowflake/02_migrate_data.py's own main() end to end against a stand-in
# Snowflake and asserts on the PROCESS EXIT CODE and the text the push
# actually prints.
#
# WHY THAT DISTINCTION IS THE WHOLE POINT. The guard itself is strong: 14 of
# 16 mutations inside push_verify.py are caught. The WIRING around it was not.
# Six one-line edits to 02_migrate_data.py left every test in this file green,
# and three of them made the push print the per-column mismatch in full and
# then exit 0 -- which, to daily_update.ps1 and to the health ping and to the
# person reading the morning email, is indistinguishable from deleting the
# guard. Every test covering the wiring was an AST or source-text presence
# assertion, and those prove the code LOOKS right; they cannot prove it
# BEHAVES right. CLAUDE.md already records three guards of exactly that class:
# "If you add a guard, prove it fails on bad input before trusting it."
#
# The surviving one-line edits, and the test that now kills each:
#
#   W1a  drop `verdict.status == "MISMATCH"` from line 230
#        -> test_a_value_only_corruption_exits_3_from_the_real_push
#   W1b  drop `status == "MISMATCH"` from line 230
#        -> test_the_row_count_keeps_its_teeth_when_the_fingerprint_cannot_run
#   W2   `content_failed.append(table)` -> `pass`
#        -> test_a_value_only_corruption_exits_3_from_the_real_push
#   W3   `critical_content = [...]` -> `[]`
#        -> test_a_value_only_corruption_exits_3_from_the_real_push
#   W4   verify_table(sqlite_conn, sqlite_conn, ...) or the two swapped
#        -> test_the_push_passes_sqlite_first_and_snowflake_second
#           and test_each_side_is_fingerprinted_with_its_own_dialect
#   W5   delete the INCONCLUSIVE warning
#        -> test_an_inconclusive_check_warns_instead_of_looking_like_a_pass
#   W6   _text_expr's 'YYYY-MM-DD' -> anything else
#        -> test_a_date_renders_identically_on_the_two_sides
#
# NOTHING HERE TOUCHES SNOWFLAKE OR data/mars_history.db. main() DELETEs every
# table it is handed, so it is pointed at a temp SQLite file on both ends and
# snowflake_db.get_conn is replaced BEFORE it runs.
# ---------------------------------------------------------------------------

CALF = "calf_sales"          # stands in for an OPTIONAL table


def _sqlite_value(v):
    """A pandas cell as sqlite3 will accept it (numpy scalars, NaN -> NULL)."""
    if v is None:
        return None
    if hasattr(v, "item"):
        v = v.item()
    if isinstance(v, float) and v != v:
        return None
    return v


class StubSnowflake:
    """
    A temp SQLite file wearing the Snowflake connection's clothes, complete
    enough to drive 02_migrate_data.py's main() from end to end: DESC TABLE,
    BEGIN/DELETE/COMMIT/ROLLBACK, the cheap SELECT COUNT(*), and TO_CHAR for
    the fingerprint's DATE branch.

    `fingerprint_boom`, when set, raises on the fingerprint's multi-aggregate
    SELECT and ONLY on it -- the cheap count still answers. That is how the
    INCONCLUSIVE path is reached without also disabling the count, which is
    the exact combination the second clause of line 230 exists for.
    """

    def __init__(self, path, types=None):
        # isolation_level=None: main() drives BEGIN/COMMIT/ROLLBACK itself and
        # sqlite3's implicit transaction handling would fight it.
        self.conn = sqlite3.connect(str(path), isolation_level=None)
        self.conn.create_function("TO_CHAR", 2, sf_to_char)
        self.types = types or SF_TYPES
        self.fingerprint_boom = None
        self.statements = []
        self.closed = False

    @staticmethod
    def _is_fingerprint(sql):
        # The fingerprint is the only multi-expression SELECT the push issues;
        # the cheap check is exactly "SELECT COUNT(*) FROM <table>".
        head = sql.split(" FROM ", 1)[0]
        return head.upper().lstrip().startswith("SELECT") and "," in head

    class _Cur:
        def __init__(self, outer):
            self.outer = outer
            self.rows = []

        def execute(self, sql):
            self.outer.statements.append(sql)
            if sql.upper().lstrip().startswith("DESC TABLE"):
                self.rows = list(self.outer.types.items())
                return self.rows
            if self.outer.fingerprint_boom and self.outer._is_fingerprint(sql):
                raise self.outer.fingerprint_boom
            self.rows = self.outer.conn.execute(sql).fetchall()
            return self.rows

        def fetchone(self):
            return self.rows[0] if self.rows else None

        def __iter__(self):
            return iter(self.rows)

    def cursor(self):
        return self._Cur(self)

    def close(self):
        self.closed = True
        self.conn.close()


def _make_db(path, tables):
    """A SQLite file holding {table: rows}, every table on the fci_daily DDL."""
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        for name, rows in tables.items():
            conn.execute(DDL.replace("fci_daily", name))
            conn.executemany(f"INSERT INTO {name} VALUES (?,?,?,?,?)", rows)
    finally:
        conn.close()


class PushRun:
    """What one main() call did: its exit code, its output, and who it called."""

    def __init__(self, code, out, stub, verify_calls, fingerprint_calls):
        self.code = code
        self.out = out
        self.stub = stub
        self.verify_calls = verify_calls
        self.fingerprint_calls = fingerprint_calls


def run_push(monkeypatch, tmp_path, *, critical=("fci_daily",), optional=(),
             rows=None, corrupt=None, keep_rows=None, fingerprint_boom=None,
             group=None, sf_types=None, verdict_status=None):
    """
    Run the REAL snowflake/02_migrate_data.py main() against two temp SQLite
    files, and return its exit code and output.

      rows            {table: [row, ...]} for the local side (default: ROWS).
                      A table listed in `critical`/`optional` but ABSENT from
                      `rows` is absent from the local database too, which is
                      how unreadable() is reached.
      corrupt         {table: "SQL"} applied to the Snowflake side during the
                      load -- the 09-29 shape: right row count, wrong values
      keep_rows       {table: n} -- write_pandas writes only the first n rows,
                      so the cheap COUNT(*) disagrees
      fingerprint_boom an exception the fingerprint SELECT raises
      sf_types        the DESC TABLE types the stub reports (default SF_TYPES)
      verdict_status  {table: status} -- the REAL verdict with only its
                      `status` swapped. This is how NO COLUMNS and a status
                      nobody has written yet are reached: both are states the
                      push must route correctly and neither can be produced
                      through write_pandas, which cannot write a row with no
                      columns. Nothing else about the verdict is faked, and
                      the assertion is still on the PROCESS EXIT CODE.

    A fresh copy of the module is loaded each time so one test's monkeypatched
    table lists cannot leak into the next.
    """
    import snowflake.connector.pandas_tools as pandas_tools
    import snowflake_db

    tables = list(critical) + list(optional)
    rows = rows or {t: ROWS for t in tables}

    local_path = tmp_path / "local.db"
    stub_path = tmp_path / "stub.db"
    _make_db(local_path, rows)
    _make_db(stub_path, {t: [] for t in tables})

    stub = StubSnowflake(stub_path, types=sf_types)
    stub.fingerprint_boom = fingerprint_boom

    def fake_write_pandas(conn, df, table, **kwargs):
        name = table.lower()
        n = (keep_rows or {}).get(name, len(df))
        sub = df.iloc[:n]
        cols = list(sub.columns)
        sql = (f"INSERT INTO {name} ({', '.join(cols)}) "
               f"VALUES ({', '.join('?' * len(cols))})")
        conn.conn.executemany(
            sql, [tuple(_sqlite_value(v) for v in r)
                  for r in sub.itertuples(index=False, name=None)])
        stmt = (corrupt or {}).get(name)
        if stmt:
            conn.conn.execute(stmt)
        return True, 1, n, None

    verify_calls, fingerprint_calls = [], []
    real_verify, real_fingerprint = pv.verify_table, pv.fingerprint

    def spy_verify(*args, **kwargs):
        verify_calls.append((args, kwargs))
        v = real_verify(*args, **kwargs)
        forced = (verdict_status or {}).get(v.table)
        return v._replace(status=forced) if forced else v

    def spy_fingerprint(conn, table, columns, dialect):
        fingerprint_calls.append((conn, table, dialect))
        return real_fingerprint(conn, table, columns, dialect)

    # Armed BEFORE main() runs, because main() sets USE_SNOWFLAKE on itself and
    # pytest's monkeypatch is what puts it back for every test after this one.
    monkeypatch.setenv("USE_SNOWFLAKE", "")
    monkeypatch.setattr(snowflake_db, "get_conn", lambda: stub)
    monkeypatch.setattr(pandas_tools, "write_pandas", fake_write_pandas)
    monkeypatch.setattr(pv, "verify_table", spy_verify)
    monkeypatch.setattr(pv, "fingerprint", spy_fingerprint)

    spec = importlib.util.spec_from_file_location("_migrate_under_test", MIGRATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "DB_PATH", local_path)
    monkeypatch.setattr(mod, "CRITICAL_TABLES", list(critical))
    monkeypatch.setattr(mod, "OPTIONAL_TABLES", list(optional))
    monkeypatch.setattr(mod, "TABLES", tables)

    buf = io.StringIO()
    code = 0
    with contextlib.redirect_stdout(buf):
        try:
            mod.main(group=group)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    return PushRun(code, buf.getvalue(), stub, verify_calls, fingerprint_calls)


# --- the control, without which every assertion below is vacuous -----------

def test_a_clean_push_exits_0_and_says_the_contents_were_checked(monkeypatch,
                                                                 tmp_path):
    """
    If the harness could not produce a PASSING push, every exit-3 assertion
    below would pass for the wrong reason -- a rig that always fails proves
    nothing about a guard.
    """
    run = run_push(monkeypatch, tmp_path)
    assert run.code == 0, run.out
    # 17 = COUNT(*), plus 5 for each of the two text/date columns, 2 for the
    # float and 2 for each of the two ints. Pinned so a column quietly
    # dropping out of the fingerprint fails here.
    assert ("fci_daily: sqlite=3 snowflake=3 [OK] [CONTENT OK] 17 aggregates "
            "over 3 rows") in run.out
    assert "CONTENT VERIFICATION FAILED" not in run.out
    assert "CONTENT MISMATCH" not in run.out


# --- W1a, W2, W3: values wrong, count right -> exit 3 ----------------------

def test_a_value_only_corruption_exits_3_from_the_real_push(monkeypatch, tmp_path):
    """
    THE 2026-09-29 SHAPE, DRIVEN THROUGH THE REAL PUSH LOOP. Snowflake ends up
    holding an index value of 337.05 where SQLite holds 337.0638, with the row
    count identical -- so the cheap check still prints [OK] and, before the
    fingerprint existed, that was the entire verification.

    This one test kills three separate one-line edits, because all three fail
    the same way: the push prints the difference in full and exits 0, which no
    caller can distinguish from a clean run.

        W1a  `if verdict.status == "MISMATCH" or status == "MISMATCH":`
             with the FIRST clause deleted
        W2   `content_failed.append(table)` -> `pass`
        W3   `critical_content = [t for t in ...]` -> `[]`

    It also kills the same-side-twice mistakes at the call site: comparing
    SQLite against SQLite makes the corrupted Snowflake side invisible and the
    push exits 0.

    ASSERT THE EXIT CODE, NOT ONLY THE TEXT. The text was already correct
    under all three edits. The exit code is what daily_update.ps1 branches on,
    what the health ping reports, and what decides whether anyone is emailed.
    """
    run = run_push(monkeypatch, tmp_path, corrupt={
        "fci_daily": "UPDATE fci_daily SET fci_value = 337.05 "
                     "WHERE report_date = '2026-09-11'"})

    assert run.code == 3, f"a corrupted critical table exited {run.code}\n{run.out}"

    # The count saw nothing -- which is the premise of the whole guard.
    assert "sqlite=3 snowflake=3 [OK]" in run.out, (
        "the fixture changed the row count, so it is re-testing what the old "
        "COUNT check already caught")
    assert "[CONTENT MISMATCH] 1 aggregate(s) differ" in run.out
    # The reported values, the right way round. This is also the SECOND place
    # verify_table's MISMATCH path is exercised: before it existed, weakening
    # test_verify_table_end_to_end_ok_and_mismatch resurrected three
    # same-side-twice mutants and the reversed reporting labels at once.
    assert (f"fci_daily.FCI_VALUE scaled-sum: snowflake={SCALED_SUM - 138} "
            f"sqlite={SCALED_SUM} (diff -138)") in run.out
    assert "fci_daily: CONTENT VERIFICATION FAILED (CRITICAL)" in run.out
    assert "ERROR: 1 CRITICAL table(s) loaded but their contents do NOT match" in run.out


def test_the_row_count_keeps_its_teeth_when_the_fingerprint_cannot_run(
        monkeypatch, tmp_path):
    """
    W1b -- THE SECOND CLAUSE OF LINE 230, PINNED ON ITS OWN.

    `if verdict.status == "MISMATCH" or status == "MISMATCH"` looks redundant:
    any count difference also moves the fingerprint's own (*, rows) aggregate,
    so the first clause would cover it. It is NOT redundant in the one case
    that matters -- when the fingerprint could not run at all. Then the verdict
    is INCONCLUSIVE, the first clause is False, and the cheap count is the only
    signal left. Deleting the second clause lets a table that loaded 2 of 3
    rows exit 0.

    So: rows lost during the load AND a fingerprint that raises. Both are
    needed, because either one alone leaves the other clause covering it.
    """
    run = run_push(monkeypatch, tmp_path,
                   keep_rows={"fci_daily": 2},
                   fingerprint_boom=RuntimeError("connection reset"))

    assert "sqlite=3 snowflake=2 [MISMATCH]" in run.out, run.out
    assert "[CONTENT INCONCLUSIVE] RuntimeError: connection reset" in run.out, (
        "the fingerprint completed, so this is no longer testing the case the "
        "second clause exists for")
    assert run.code == 3, (
        f"a critical table that lost rows exited {run.code} because the "
        f"fingerprint could not run\n{run.out}")
    assert "fci_daily: CONTENT VERIFICATION FAILED (CRITICAL)" in run.out


def test_an_optional_table_that_drifts_warns_loudly_and_still_exits_0(
        monkeypatch, tmp_path):
    """
    THE OTHER DIRECTION, AND IT IS WHY W3 IS A FILTER AND NOT A FLAG. A
    dashboard table whose contents drifted must not be reported as an index
    failure -- that is the split CLAUDE.md and 02_migrate_data.py both insist
    on, and the reason the exit-3 line reads
    `[t for t in content_failed if t in CRITICAL_TABLES]` and not
    `if content_failed`.

    Without this test, "make everything exit 3" would satisfy every other
    assertion in this block.
    """
    run = run_push(monkeypatch, tmp_path,
                   critical=("fci_daily",), optional=(CALF,),
                   corrupt={CALF: f"UPDATE {CALF} SET total_head = 1"})

    assert run.code == 0, f"a non-critical drift failed the index push\n{run.out}"
    assert f"{CALF}: CONTENT VERIFICATION FAILED (non-critical)" in run.out
    assert "non-critical table(s) loaded but do NOT match local SQLite" in run.out
    assert "may be showing wrong values, not stale ones" in run.out
    assert "ERROR:" not in run.out, "a dashboard tab is not an index failure"
    # and the index itself was still really checked, not skipped
    assert "fci_daily: sqlite=3 snowflake=3 [OK] [CONTENT OK]" in run.out


# --- W4: which connection goes where ---------------------------------------

def test_the_push_passes_sqlite_first_and_snowflake_second(monkeypatch, tmp_path):
    """
    W4 -- THE TWO CONNECTION ARGUMENTS, ASSERTED.

    `verify_table(sqlite_conn, sqlite_conn, table)`, `(sf_conn, sf_conn, ...)`
    and the two swapped all left the suite green. In the live push they
    degrade to INCONCLUSIVE rather than to a false [CONTENT OK], but only
    because the two connection objects happen to have different APIs -- SQLite
    has .execute(), the connector's has only .cursor(). That is an accident of
    two libraries, not a defence, and the day someone wraps the Snowflake
    connection in something that also exposes .execute(), comparing a side
    with itself becomes a silent permanent pass.

    So assert the call site directly, on a run that really happened.
    """
    run = run_push(monkeypatch, tmp_path)
    assert len(run.verify_calls) == 1, run.verify_calls
    args, kwargs = run.verify_calls[0]
    assert kwargs == {}, "the two connections must stay positional and ordered"
    sqlite_conn, sf_conn, table = args
    assert table == "fci_daily"
    assert isinstance(sqlite_conn, sqlite3.Connection), (
        "the FIRST argument is the local side and it is not a sqlite3 "
        "connection")
    assert sf_conn is run.stub, (
        "the SECOND argument is not the Snowflake connection the push opened")
    assert sqlite_conn is not sf_conn, (
        "the same connection was handed in twice -- the check would be "
        "comparing a side against itself and could never fail")


def test_each_side_is_fingerprinted_with_its_own_dialect(monkeypatch, tmp_path):
    """
    The other half of W4, one level down.

    test_the_two_dialects_do_not_generate_the_same_sql is billed as THE
    structural defence against feeding one side in twice -- but it tests
    fingerprint_sql, not the function that calls it, and both of its fixtures
    are SQLite. Passing SQLITE where SNOWFLAKE belongs inside verify_table
    therefore survives the entire suite: the Snowflake side would be
    fingerprinted with SQLite's quoting and SQLite's integer cast, and every
    float column would report a permanent false mismatch on correct data --
    the "cries wolf until somebody switches it off" failure this codebase has
    already written down twice.

    Asserted on a real run: two fingerprints per table, each connection with
    its own dialect, in order.
    """
    run = run_push(monkeypatch, tmp_path)
    assert len(run.fingerprint_calls) == 2, run.fingerprint_calls
    (lite_conn, t1, lite_dialect), (snow_conn, t2, snow_dialect) = run.fingerprint_calls
    assert (t1, t2) == ("fci_daily", "fci_daily")
    assert lite_dialect == pv.SQLITE and snow_dialect == pv.SNOWFLAKE
    assert isinstance(lite_conn, sqlite3.Connection)
    assert snow_conn is run.stub
    assert lite_conn is not snow_conn


# --- W5: a check that could not run must not look like one that passed -----

def test_an_inconclusive_check_warns_instead_of_looking_like_a_pass(
        monkeypatch, tmp_path):
    """
    W5 -- DELETING THE `elif verdict.status == "INCONCLUSIVE"` BLOCK LEFT THE
    SUITE GREEN.

    It never touches the exit code, so nothing outside the push log consumed
    it, so no test noticed. Combined with W4 -- a check that is INCONCLUSIVE on
    every table on every run -- the push exits 0 forever while verifying
    nothing: precisely the "silently degrade back to the COUNT check" this
    module was built to prevent.

    The exit code stays 0 here and that is deliberate (see the test below). The
    push must still SAY so, and must not say [CONTENT OK].
    """
    run = run_push(monkeypatch, tmp_path,
                   fingerprint_boom=RuntimeError("connection reset"))

    assert run.code == 0, (
        "a Snowflake hiccup in the CHECK must not fail a push whose index "
        "already committed")
    assert "[CONTENT INCONCLUSIVE] RuntimeError: connection reset" in run.out
    assert "content verification could not run" in run.out, (
        "the INCONCLUSIVE warning is gone; a check that never ran now prints "
        "the same as one that passed")
    assert "UNVERIFIED, not confirmed" in run.out
    assert "[CONTENT OK]" not in run.out, (
        "inconclusive and verified must never print the same way")
    assert "CONTENT VERIFICATION FAILED" not in run.out, (
        "a check that could not run is not a check that failed")


def test_a_critical_table_left_unverified_is_named_in_the_run_summary(
        monkeypatch, tmp_path):
    """
    THE JUDGEMENT ON W5, PINNED.

    Should a CRITICAL table being INCONCLUSIVE be visible outside the
    per-table log line? The answer taken here is YES, VISIBLY, BUT NOT AS A
    FAILURE.

    AGAINST raising the exit code: a check that cannot run is not a check that
    failed. The index is already COMMITTED by the time the fingerprint runs,
    and a Snowflake hiccup in the verification would become exit != 0, which
    daily_update.ps1 reads as "the DASHBOARD IS STALE" and mails a failure
    about an index that published perfectly. CLAUDE.md's own words: "calling
    this a failure would train someone to ignore a real one." So the exit code
    is NOT changed.

    FOR making it visible: the per-table line is one of twenty in a long log
    and it did not distinguish fci_daily from hay_bids -- the MISMATCH line
    says (CRITICAL) or (non-critical) and the INCONCLUSIVE line said neither.
    So it now names the scope, and a run that could not verify a CRITICAL
    table ends with a WARNING block beside the two that already exist.
    check_run.ps1's digest filters the log on 'WARN', so that block is what a
    human actually sees the next morning: visibility without turning a
    transient into an index failure.

    What is deliberately NOT built: per-run state. "INCONCLUSIVE every run for
    a week" is the case that should escalate, and the push has no memory to
    detect it with. A WARNING block that keeps reappearing in the daily digest
    is the honest substitute.
    """
    run = run_push(monkeypatch, tmp_path,
                   critical=("fci_daily",), optional=(CALF,),
                   fingerprint_boom=RuntimeError("connection reset"))

    assert run.code == 0
    assert "fci_daily: WARNING - content verification could not run (CRITICAL)" in run.out
    assert f"{CALF}: WARNING - content verification could not run (non-critical)" in run.out
    assert ("WARNING: 1 CRITICAL table(s) loaded but their contents could NOT "
            "be verified") in run.out
    tail = run.out.split("could NOT be verified", 1)[1][:300]
    assert "fci_daily" in tail
    assert CALF not in tail, (
        "the summary block must name CRITICAL tables only -- a dashboard tab "
        "going unverified is not the index going unverified")


def test_the_unverified_summary_stays_quiet_when_everything_was_checked(
        monkeypatch, tmp_path):
    """The block above must not fire on a clean run, or it is wallpaper."""
    run = run_push(monkeypatch, tmp_path)
    assert "could NOT be verified" not in run.out
    assert "content verification could not run" not in run.out


# --- W6: the pinned date format --------------------------------------------

def test_a_date_renders_identically_on_the_two_sides():
    """
    W6 -- THE FORMAT STRING ITSELF.

    TO_CHAR(col, 'YYYY-MM-DD') is what makes a Snowflake DATE render the same
    as a SQLite TEXT date under CAST, and it is pinned rather than left to the
    session's DATE_OUTPUT_FORMAT so an account-wide setting change cannot
    shift all 33 DATE columns' length sums at once and scream about an index
    that is fine.

    Changing it to 'DD-MM-YYYY' passed all 33 tests, because the only
    assertion anywhere was that the substring "TO_CHAR" appeared. The real
    consequence is that the two sides stop being comparable: same length, so
    len-sum still agrees, but min64/max64 diverge and every DATE column
    reports a permanent mismatch on a correct push. So assert the literal
    format, and (next test) assert the two expressions really do agree.
    """
    col = pv.Column("REPORT_DATE", "report_date", "DATE", pv.classify("DATE"))
    assert pv._text_expr(col, pv.SNOWFLAKE) == "TO_CHAR(REPORT_DATE, 'YYYY-MM-DD')"
    assert pv._text_expr(col, pv.SQLITE) == 'CAST("report_date" AS VARCHAR)'


def test_the_two_date_expressions_agree_value_for_value(pair):
    """
    The claim the format string rests on, exercised rather than asserted in a
    comment: for the same date, the SQLite expression and the Snowflake
    expression return the same string.
    """
    a, b = pair
    col = pv.Column("REPORT_DATE", "report_date", "DATE", pv.classify("DATE"))
    sf = FakeSnowflake(b)
    lite = [r[0] for r in a.execute(
        f"SELECT {pv._text_expr(col, pv.SQLITE)} FROM fci_daily ORDER BY 1")]
    snow = [r[0] for r in sf.cursor().execute(
        f"SELECT {pv._text_expr(col, pv.SNOWFLAKE)} FROM fci_daily ORDER BY 1")]
    assert lite == ["2026-09-11", "2026-09-14", "2026-09-15"], (
        "the SQLite side no longer renders a date as YYYY-MM-DD, so the "
        "pinned Snowflake format is pinned to the wrong thing")
    assert snow == lite, (
        "the two sides render the same date differently, so every DATE "
        "column's prefix extremes would mismatch on a correct push")


# ---------------------------------------------------------------------------
# W7 -- THE VERDICTS THAT ARE NEITHER "MISMATCH" NOR "INCONCLUSIVE".
#
# Found by mutating the wiring again after W1-W6 were pinned.
# 02_migrate_data.py's own module docstring documents exit 3 as
#
#     "every table loaded, but a critical table's contents were not CONFIRMED
#      to match local SQLite -- either they demonstrably differ, or nothing
#      about them was verified at all (0 rows, or no column shared between the
#      two schemas)"
#
# and push_verify.Verdict.verified's docstring says "02_migrate_data.py
# branches on it -- before that it had no caller anywhere outside the tests,
# which is how EMPTY on a CRITICAL table exited 0."
#
# BOTH SENTENCES WERE FALSE. main() read `verdict.status` and never
# `verdict.verified`, so EMPTY and NO COLUMNS on a CRITICAL table printed
# their honest one-line summary and exited 0. The promised guard did not
# exist, and nothing could have failed to notice, because nothing looked.
# Same un-failable shape as the six edits above, one level out: not a guard
# that can be deleted silently, a guard that was never wired at all while two
# docstrings said it was.
#
# The branch is keyed on `verified`, which is default-deny: a status added
# later is not-verified until someone says otherwise, so it exits 3 rather
# than passing by omission. INCONCLUSIVE is handled ABOVE it and keeps exit 0
# -- a check that could not run is not a check that failed.
# ---------------------------------------------------------------------------

def test_an_empty_critical_table_exits_3_and_does_not_pass(monkeypatch, tmp_path):
    """
    THE SHAPE THAT MOTIVATES IT. fci_daily goes to 0 rows locally -- a bad
    recompute, a wrong window, a truncated working copy. The push DELETEs
    Snowflake's 955 rows and reloads nothing. Both sides now hold 0 rows, so
    every aggregate is NULL == NULL, the cheap count agrees at 0 == 0, and the
    verdict is EMPTY: nothing whatsoever was verified.

    Before this branch existed the push printed
    "[OK] [CONTENT EMPTY] 0 rows both sides - nothing was verified" and exited
    0, so daily_update.ps1 reported a clean run over a dashboard that now
    serves no index at all. That is strictly worse than the stale-but-correct
    state exit 1 describes, which is why it gets exit 3 like any other
    committed-but-unconfirmed write.
    """
    run = run_push(monkeypatch, tmp_path, rows={"fci_daily": []})

    assert "fci_daily: sqlite=0 snowflake=0 [OK]" in run.out, (
        "the row count agreed, which is the premise: the cheap check sees "
        "nothing wrong with an index that vanished")
    assert "[CONTENT EMPTY] 0 rows both sides - nothing was verified" in run.out
    assert run.code == 3, (
        f"an EMPTY critical table exited {run.code}; the module docstring "
        f"promises 3 for 'nothing about them was verified at all'\n{run.out}")
    assert "fci_daily: CONTENT NOT CONFIRMED (CRITICAL)" in run.out
    assert ("ERROR: 1 CRITICAL table(s) loaded but nothing about their "
            "contents was confirmed") in run.out
    # EMPTY is not MISMATCH and must not borrow its sentence: the two sides
    # agree perfectly, there is just nothing there.
    assert "do NOT match local SQLite" not in run.out, (
        "an empty table matches local SQLite exactly; saying otherwise sends "
        "somebody hunting a difference that does not exist")


def test_a_critical_table_with_no_shared_column_exits_3(monkeypatch, tmp_path):
    """
    The other half of the documented exit-3 clause, driven through main().

    NO COLUMNS cannot be produced through write_pandas -- a DataFrame with no
    columns writes no rows -- so the real verdict's status is swapped and
    nothing else about it is. What is tested here is the WIRING: given a
    verdict that confirmed nothing, does the process exit 3? Whether
    verify_table produces that status correctly is
    test_a_table_whose_schemas_share_no_column_is_not_a_pass, above.
    """
    run = run_push(monkeypatch, tmp_path,
                   verdict_status={"fci_daily": "NO COLUMNS"})

    assert "[CONTENT NO COLUMNS]" in run.out
    assert run.code == 3, (
        f"a critical table whose schemas share no column exited {run.code}; "
        f"only COUNT(*) was compared, which is the 2026-09-29 blind spot\n"
        f"{run.out}")
    assert "fci_daily: CONTENT NOT CONFIRMED (CRITICAL)" in run.out


def test_a_status_nobody_has_written_yet_is_not_a_pass(monkeypatch, tmp_path):
    """
    DEFAULT-DENY, WHICH IS THE WHOLE REASON THE BRANCH READS
    `not verdict.verified` AND NOT A LIST OF STATUS NAMES.

    Verdict.verified is documented as "a status added later is not-verified by
    default rather than a silent pass". That is only true if the push agrees,
    and a branch spelled `elif verdict.status in ("EMPTY", "NO COLUMNS")`
    would satisfy both tests above while letting the next status through with
    exit 0. So: hand the push a status this file invented, and require it to
    refuse it.
    """
    run = run_push(monkeypatch, tmp_path,
                   verdict_status={"fci_daily": "PARTIALLY LOOKED AT"})

    assert run.code == 3, (
        f"a status the push has never seen exited {run.code}. The branch is "
        f"matching status names instead of asking whether anything was "
        f"verified, so the next status added passes by omission\n{run.out}")
    assert "fci_daily: CONTENT NOT CONFIRMED (CRITICAL)" in run.out


def test_an_empty_optional_table_stays_quiet_and_exits_0(monkeypatch, tmp_path):
    """
    THE COUNTERWEIGHT, AND IT IS NOT HYPOTHETICAL: mars_census is 0 rows on
    both sides every single day and is OPTIONAL for exactly that reason.

    If EMPTY were escalated regardless of scope, this push would emit a
    content failure every morning forever -- CLAUDE.md's "calling this a
    failure would train someone to ignore a real one", arrived at from the
    other direction. And it must not join the "loaded but do NOT match local
    SQLite" block either: an empty table matches local SQLite exactly.

    Without this test, "escalate every unverified table" satisfies the three
    above.
    """
    run = run_push(monkeypatch, tmp_path,
                   critical=("fci_daily",), optional=(CALF,),
                   rows={"fci_daily": ROWS, CALF: []})

    assert run.code == 0, f"an empty dashboard table failed the index push\n{run.out}"
    assert f"{CALF}: sqlite=0 snowflake=0 [OK] [CONTENT EMPTY]" in run.out, (
        "the empty optional table must still SAY nothing was verified")
    assert "CONTENT NOT CONFIRMED" not in run.out
    assert "ERROR:" not in run.out
    # None of the end-of-run blocks may fire. Asserted by phrase and not by
    # the bare word 'WARN', because bound_session legitimately prints one
    # about the session's own timeouts, and a test that forbids the whole
    # word would fail on an unrelated correct line.
    for block in ("loaded but do NOT match local SQLite",
                  "nothing about their contents was confirmed",
                  "content verification could not run",
                  "failed and were skipped"):
        assert block not in run.out, (
            f"a table that is empty by design put {block!r} in the log; "
            f"check_run.ps1's digest filters on 'WARN' and would show it "
            f"every single morning")
    # and the index itself was really checked
    assert "fci_daily: sqlite=3 snowflake=3 [OK] [CONTENT OK]" in run.out


# ---------------------------------------------------------------------------
# THE TWO MUTANTS THAT STILL SURVIVE, AND WHY NEITHER IS PINNED.
#
# Writing them down so the next person does not spend an afternoon rediscovering
# them, and does not "fix" them with an assertion that cannot fail.
#
#   `failed_optional.extend(t for t in optional_content ...)` -> `pass`
#       Survives because it has NO OBSERVABLE EFFECT. Nothing reads
#       failed_optional after that line -- skipped_optional was already
#       snapshotted from it, the two WARNING blocks read skipped_optional and
#       optional_content, and main() returns None. Its comment says the tables
#       "join failed_optional for any caller reading it"; there is no such
#       caller. A test cannot pin an effect that does not happen, so the honest
#       fixes are to give main() a return value somebody uses, or to delete the
#       line. Asserting on it via AST would be exactly the un-failable guard
#       this whole file exists to replace.
#
#   `Verdict.verified`'s `and self.rows > 0` -> dropped
#       An EQUIVALENT MUTANT under the current verify_table: status can only be
#       "OK" when rows > 0. rows == 0 on both sides is EMPTY, and rows == 0 on
#       one side only moves the (*, rows) aggregate and is a MISMATCH. The
#       clause is correct defensive code for a status added later, not a live
#       guard, and there is no input that distinguishes the two versions.
#
# ---------------------------------------------------------------------------
# W8 / W9 -- two more survivors of the same mutation pass, both test-only.
# ---------------------------------------------------------------------------

def test_the_push_names_a_column_it_cannot_fingerprint(monkeypatch, tmp_path):
    """
    W8 -- `if verdict.unclassified:` -> `if False:` left the suite green.

    push_verify's own test_an_unknown_type_is_named_out_loud asserts the
    VERDICT carries the column. Nothing asserted the push ever prints it, so
    the line that tells a human "these values are NOT verified" could be
    deleted without a single test moving. A column whose Snowflake type this
    check cannot fingerprint degrades to "row presence checked, values not" --
    which is acceptable, and is acceptable only because it is said out loud.
    """
    run = run_push(monkeypatch, tmp_path,
                   sf_types=dict(SF_TYPES, LOCATION="GEOGRAPHY"))

    assert run.code == 0, run.out
    assert "LOCATION" in run.out and "their values are NOT verified" in run.out, (
        "the push no longer names a column it could not fingerprint, so a "
        "type it cannot read is indistinguishable from one it checked")
    # The rest of the table was still verified, so this must not read as a
    # blanket failure either.
    assert "[CONTENT OK]" in run.out


def test_an_optional_table_missing_locally_reaches_the_morning_digest(
        monkeypatch, tmp_path):
    """
    W9 -- `failed_optional.append(table)` in the unreadable() skip branch ->
    `pass` left the suite green.

    The per-table "SKIPPED" line still printed, so the log looked right. What
    it lost was the end-of-run block, and that block is the only part a human
    sees: check_run.ps1's morning digest filters the log on 'WARN', and the
    SKIPPED line does not contain it. So a dashboard table absent from every
    push would disappear silently and indefinitely.

    This is the mars_census shape exactly -- update_index.py's census call is
    deliberately allowed to fail without failing the run, which leaves the
    table absent locally.
    """
    run = run_push(monkeypatch, tmp_path,
                   critical=("fci_daily",), optional=(CALF,),
                   rows={"fci_daily": ROWS})

    assert run.code == 0, f"a missing dashboard table failed the index push\n{run.out}"
    assert f"{CALF}: SKIPPED - absent from SQLite" in run.out
    assert (f"WARNING: 1 non-critical table(s) failed and were skipped: "
            f"{CALF}") in run.out, (
        "the skipped table never reaches the end-of-run WARNING block, which "
        "is the only line check_run.ps1's digest will show anybody")
    assert "will be stale" in run.out
    assert "fci_daily: sqlite=3 snowflake=3 [OK] [CONTENT OK]" in run.out
