"""
The MEASUREMENTS push_verify.py's docstrings state, re-derived from the live
database and checked against the text.

WHY THIS FILE EXISTS. push_verify.py's docstrings are not decoration: they
carry the argument for why the float sums are integer-scaled, and that
argument sits directly above an operational instruction ("DO NOT SIMPLIFY THE
ROUND AWAY"). A reader deciding whether to believe that instruction has
nothing to go on but the numbers next to it.

Twice now those numbers have been wrong. The paragraph on .5 boundaries said
"26 float columns" when the schema has 35 -- nobody had counted. The paragraph
on integer scaling asserted that a raw SUM of floats is order-dependent across
the two engines; that came from a design brief, was never measured, and is
false on both backends. Each error was reassuring rather than alarming, which
is the dangerous direction: they made the module look better checked than it
was.

CLAUDE.md names the remedy: "comparing a number against an independently
derived number", and "if you add a guard, prove it fails on bad input". So
every assertion here RE-DERIVES the figure from data and then looks for it in
the prose. Editing either the docstring or the schema without the other going
red is the failure this prevents.

WHAT IS PINNED AND WHAT IS DELIBERATELY NOT. Only figures that are properties
of the SCHEMA or of the two engines' ARITHMETIC are pinned -- how many float
columns there are, how many of them the outer ROUND changes, which way the
error leans, and the .5-boundary census. Those move when something real
changes, which is exactly when a human should re-read the paragraph.

Row-level counts ("2,710 of 72,641 rows diverge") are NOT pinned. They grow
every day the pipeline runs, so a test on them would fail every morning for a
benign reason, and push_verify.py's own docstring now argues that an alert
which is wrong every day is one that gets switched off before the day it is
right. Those figures are dated in the prose instead.

NOTHING HERE WRITES ANYTHING. SQLite is opened mode=ro and Snowflake is not
contacted at all: the float classification is taken from SQLite's own REAL
declared types, which was verified against Snowflake's DESC TABLE on
2026-09-30 to select the identical 35 columns. That keeps the suite runnable
with no credentials and off the index's critical path.
"""
import math
import re
import sqlite3
import sys
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import push_verify as pv                                    # noqa: E402

DB = REPO / "data" / "mars_history.db"

# The tables the push actually uploads, read from 02_migrate_data.py rather
# than copied, so a table added there is measured here with no edit -- the
# same rule the module docstring cites from CLAUDE.md.
MIGRATE = REPO / "snowflake" / "02_migrate_data.py"


def pushed_tables():
    src = MIGRATE.read_text(encoding="utf-8")
    names = []
    for listname in ("CRITICAL_TABLES", "OPTIONAL_TABLES"):
        m = re.search(rf"^{listname}\s*=\s*\[(.*?)\]", src, re.S | re.M)
        if not m:
            pytest.skip(f"cannot find {listname} in 02_migrate_data.py")
        names += re.findall(r'"([a-z_]+)"', m.group(1))
    return names


@pytest.fixture(scope="module")
def conn():
    if not DB.exists():
        pytest.skip("no local database")
    c = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    yield c
    c.close()


@pytest.fixture(scope="module")
def float_columns(conn):
    """
    (table, column) for every float-class column the push uploads.

    Classified from SQLite's REAL declared type. push_verify classifies from
    Snowflake's DESC TABLE instead, and the two agree on the whole schema --
    verified live 2026-09-30, both selecting the same 35 columns.
    """
    out = []
    for t in pushed_tables():
        try:
            info = conn.execute(f"PRAGMA table_info({t})").fetchall()
        except sqlite3.Error:                       # table not local yet
            continue
        for row in info:
            if (row[2] or "").upper().startswith("REAL"):
                out.append((t, row[1]))
    if not out:
        pytest.skip("no float columns found locally")
    return out


def half_away(x: float) -> int:
    """Snowflake's CAST(<float> AS BIGINT): round half away from zero.

    Modelled rather than queried so the suite needs no credentials. Verified
    against the live engine on 2026-09-30 -- CAST(TO_DOUBLE(2.9) AS BIGINT) is
    3, CAST(TO_DOUBLE(-2.9) AS BIGINT) is -3 -- and the model reproduces the
    live per-column results exactly (21 affected columns, 20 up and 1 down).
    """
    return int(Decimal(x).quantize(Decimal(1), rounding=ROUND_HALF_UP))


@pytest.fixture(scope="module")
def round_effect(conn, float_columns):
    """
    {(table, column): snowflake_sum - truncating_sum} for the scaled column.

    The delta IS the cost of dropping the outer ROUND: SQLite truncates and
    Snowflake rounds, so a non-zero entry is a column that would mismatch on
    every push for arithmetic reasons alone.
    """
    out = {}
    for t, c in float_columns:
        delta = 0
        for (v,) in conn.execute(f'SELECT "{c}" FROM {t} WHERE "{c}" IS NOT NULL'):
            try:
                s = float(v) * pv.FLOAT_SCALE
            except (TypeError, ValueError):
                continue
            if math.isfinite(s):
                delta += half_away(s) - math.trunc(s)
        out[(t, c)] = delta
    return out


DOC = pv.__doc__
CAST_DOC = pv._int_cast.__doc__


def flat(s: str) -> str:
    """Whitespace-collapsed text, so a phrase still matches when the prose
    wraps it across two lines. Without this the tests would be pinning the
    line breaks as much as the numbers."""
    return re.sub(r"\s+", " ", s)


FLAT_DOC = flat(DOC)
FLAT_CAST = flat(CAST_DOC)


def census_block() -> str:
    """Just the boundary table -- the indented 'value -> scaled  LABEL' lines.

    Counting labels over the whole docstring would also count the section
    heading that uses the same words, which is how the first draft of this
    file managed to fail on correct prose.
    """
    return "\n".join(ln for ln in DOC.splitlines()
                     if "->" in ln and "both engines" in ln)


# ---------------------------------------------------------------------------
# H1 -- the .5-boundary paragraph.
# ---------------------------------------------------------------------------

def test_the_float_column_count_in_the_prose_is_the_schemas_own(float_columns):
    """
    The original error: the paragraph said 26 across a schema with 35. It was
    load-bearing -- the sentence exists to reassure the reader that the
    boundary hazard has been surveyed, and a survey of the wrong population
    reassures about nothing.
    """
    n = len(float_columns)
    for phrase in (
        f"over all {n} float columns in the schema",
        f"matched the forward sum on {n} of {n} columns",
        f"matched the flat sum on {n} of {n}",
        f"all {n} float columns",
    ):
        assert flat(phrase) in FLAT_DOC, (
            f"the schema has {n} float columns but the docstring does not say "
            f"so: expected to find {phrase!r}. Re-derive the paragraph."
        )
    assert f"0 of the {n} float columns' sums" in FLAT_CAST
    assert re.search(r"\b26 float columns\b", FLAT_DOC) is None, (
        "the stale '26 float columns' figure is back")


def test_the_boundary_census_matches_the_data(conn, float_columns):
    """
    Every value within 1e-6 of a .5 boundary, which are exact halves and which
    are not. The paragraph called all three exact halves; one is a single ULP
    BELOW the boundary, which is the precise shape the same paragraph warns
    about, so the error emptied the warning of its only real example.
    """
    near = []
    for t, c in float_columns:
        for (v,) in conn.execute(f'SELECT "{c}" FROM {t} WHERE "{c}" IS NOT NULL'):
            try:
                s = float(v) * pv.FLOAT_SCALE
            except (TypeError, ValueError):
                continue
            if math.isfinite(s) and abs(s - math.floor(s) - 0.5) <= 1e-6:
                near.append((t, c, float(v), s))

    halves = [n for n in near if n[3] == math.floor(n[3]) + 0.5]
    others = [n for n in near if n not in halves]
    block = census_block()

    # Every such value must appear in the census, with its scaled form.
    for t, c, v, s in near:
        assert repr(v) in block, (
            f"{t}.{c} = {v!r} sits within 1e-6 of a .5 boundary and the "
            f"docstring's census does not list it")
        assert repr(s) in block, (
            f"{v!r} scales to {s!r}, which the docstring's census omits")

    assert len(block.splitlines()) == len(near), (
        f"{len(near)} values sit within 1e-6 of a .5 boundary; the census "
        f"lists {len(block.splitlines())}")
    assert block.count("exact half") == len(halves), (
        f"{len(halves)} of the boundary values are exact halves; the "
        f"census labels {block.count('exact half')}")
    assert block.count("ONE ULP BELOW") == len(others), (
        f"{len(others)} boundary value(s) are NOT exact halves; the census "
        f"labels {block.count('ONE ULP BELOW')}. Calling a near-miss an exact "
        f"half is what made this paragraph reassuring and wrong.")

    # ZERO is now the expected count, and the assertion below must tolerate it.
    # On 2026-09-30 three values sat near a boundary, all in
    # cme_ftp_brackets.avg_weight. All three turned out to be PARSER ARTEFACTS:
    # cme_ftp.py's token path glued two columns, so "782.93 365..." became the
    # single value 782.93365, and the five-decimal precision that put them on a
    # boundary was never in CME's file. The 2026-10-05 slice-first fix removed
    # them. Asserting "exactly one column" was right about the data and wrong as
    # an invariant -- it fails the moment the data gets CLEANER, which is the
    # worst time for a test to go red.
    tables = {f"{t}.{c}" for t, c, _, _ in near}
    if not near:
        return
    assert len(tables) == 1 and "cme_ftp_brackets.avg_weight" in tables, (
        f"the boundary values are no longer confined to "
        f"cme_ftp_brackets.avg_weight: {sorted(tables)}")


# ---------------------------------------------------------------------------
# H2 -- why the sums are integer-scaled.
# ---------------------------------------------------------------------------

def test_the_order_dependence_story_stays_dead(conn, float_columns):
    """
    The claim that was never checked: that a raw float SUM differs between the
    engines run to run, so the check "would cry wolf forever". SQLite's SUM is
    order-invariant -- proved here by reversing the scan on every float column.

    If this ever fails, the docstring's disproof is what is wrong, not the
    scaling; the scaling is justified by the CAST difference below.
    """
    for t, c in float_columns:
        fwd = conn.execute(f'SELECT SUM("{c}") FROM {t}').fetchone()[0]
        rev = conn.execute(
            f'SELECT SUM(x) FROM (SELECT "{c}" AS x FROM {t} ORDER BY rowid DESC)'
        ).fetchone()[0]
        assert fwd == rev, (
            f"{t}.{c}: SQLite's SUM moved when the scan was reversed "
            f"({fwd!r} vs {rev!r}). The docstring says it does not.")

    assert "IT IS NOT WHAT EITHER ENGINE DOES" in FLAT_DOC
    for dead in ("parallelises the aggregation across micro-partitions while SQLite\n"
                 "does not, and that the two would therefore differ in the last bits",):
        # The story may only appear as the thing being refuted.
        idx = FLAT_DOC.find("order-dependent")
        assert idx != -1 and "USED TO GIVE" in FLAT_DOC[:idx], (
            "order-dependence is stated as fact again rather than as the "
            "refuted claim")


def test_the_round_is_a_noop_on_one_engine_and_load_bearing_on_the_other(
        round_effect, float_columns):
    """
    The replacement reason, and the one the instruction rests on. Truncation
    vs round-half-away is a property of the two CASTs, so it reproduces on
    demand -- unlike the order-dependence story it replaced.
    """
    n = len(float_columns)
    affected = {k: v for k, v in round_effect.items() if v}
    assert affected, (
        "no float column is affected by the outer ROUND, so the docstring's "
        "entire justification for it is now false")

    for phrase in (f"it moves {len(affected)} of the {n}",
                   f"breaks {len(affected)} of the {n} float columns"):
        assert flat(phrase) in FLAT_DOC or flat(phrase) in FLAT_CAST, (
            f"dropping the ROUND affects {len(affected)} of {n} float "
            f"columns; the docstrings do not say so (wanted {phrase!r})")


def test_the_error_does_not_always_lean_the_same_way(round_effect):
    """
    The third wrong measurement: "always with Snowflake reading higher".
    Truncation moves toward ZERO, so a column holding negatives leans the
    other way, and the schema has exactly one. It matters because that column
    also near-cancels -- a two-unit discrepancy reads as noise, not as the
    systematic breakage it is.
    """
    up = {k for k, v in round_effect.items() if v > 0}
    down = {k for k, v in round_effect.items() if v < 0}
    assert down, (
        "no column leans the other way any more; the docstring's correction "
        "of 'always higher' is now itself wrong")

    assert f"higher on {len(up)} of the {len(up) + len(down)}" in FLAT_DOC
    assert (f"higher on {len(up)} of the {len(up) + len(down)} columns"
            in FLAT_CAST)

    for t, c in down:
        assert f"{t}.{c}" in FLAT_DOC and f"{t}.{c}" in FLAT_CAST, (
            f"{t}.{c} is the column where Snowflake reads LOWER and neither "
            f"docstring names it")

    assert not re.search(r"always with Snowflake reading higher", FLAT_DOC), (
        "the 'always higher' claim is back; it is false for any column "
        "holding negative values")


def test_the_alternative_that_looks_equivalent_is_not(conn, float_columns):
    """
    ROUND(col, 4) * 10000 rounds the UNSCALED value, so the residue is scaled
    back up afterwards. The docstring says it leaves the same columns
    disagreeing; this checks the count it quotes.
    """
    bad = [f"{t}.{c}" for t, c in float_columns
           if conn.execute(
               f'SELECT SUM(CAST(ROUND("{c}"*{pv.FLOAT_SCALE}) AS INTEGER)) FROM {t}'
           ).fetchone()[0]
           != conn.execute(
               f'SELECT SUM(CAST(ROUND("{c}",4)*{pv.FLOAT_SCALE} AS INTEGER)) FROM {t}'
           ).fetchone()[0]]
    assert f"the same {len(bad)} columns disagreeing" in FLAT_DOC, (
        f"ROUND(col,4)*{pv.FLOAT_SCALE} breaks {len(bad)} columns; the "
        f"docstring quotes a different number")


# ---------------------------------------------------------------------------
# H3 -- what the check cannot do.
# ---------------------------------------------------------------------------

def test_the_docstring_admits_the_three_things_this_cannot_do():
    """
    Each of these is a way the check can print [CONTENT OK] over a real
    problem. A reader who does not know them will over-trust the [OK] line,
    which is the exact failure the COUNT(*) check had.
    """
    gaps = {
        "it only runs at push time":
            ("ONLY RUNS AT PUSH TIME", "IS NOT EXONERATING"),
        "the two backends legitimately differ between pushes":
            ("between pushes the two\n    backends are SUPPOSED to differ",),
        "it is not an internal-consistency check":
            ("NOT AN INTERNAL-CONSISTENCY ONE", "own mars_sales"),
        "a fingerprint is not a hash":
            ("FINGERPRINT, NOT A HASH", "no MD5/SHA"),
    }
    for gap, needles in gaps.items():
        for needle in needles:
            assert flat(needle) in FLAT_DOC, (
                f"the docstring no longer states that {gap}")
