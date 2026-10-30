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
error leans, and whether the two engines read any float value differently.
Those move when something real changes, which is exactly when a human should
re-read the paragraph.

The .5-boundary census WAS on that list and was taken off on 2026-10-26. It
turned out not to be a property of the schema at all: fci_daily's
same_day_avg_weight lands on an exact half whenever the day's head count
carries enough factors of two, so the census was pinning a figure that
changes with the morning's sales -- a row-level count wearing a schema
claim's clothes. It is replaced by the question it was standing in for,
which is whether SQLite and Snowflake round any value differently, and that
one is stable.

Row-level counts ("2,710 of 72,641 rows diverge") are NOT pinned. They grow
every day the pipeline runs, so a test on them would fail every morning for a
benign reason, and push_verify.py's own docstring now argues that an alert
which is wrong every day is one that gets switched off before the day it is
right. Those figures are dated in the prose instead.

NOTHING HERE WRITES ANYTHING. SQLite is opened mode=ro and Snowflake is not
contacted at all: the float classification is taken from SQLite's own REAL
declared types, which was verified against Snowflake's DESC TABLE on
2026-09-30 to select the identical 35 columns, and again on 2026-10-30 at 46
after the cme_archive pair joined the push. That keeps the suite runnable
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
    verified live 2026-09-30, both selecting the same 35 columns, and again
    2026-10-30 at 46. The count is NOT pinned in this docstring on purpose:
    the tests below re-derive it from this fixture and then look for it in
    push_verify.py's prose, so a schema that grows fails the docstring that
    makes a claim about it rather than this sentence.
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
    live per-column results exactly: 21 affected columns, 20 up and 1 down
    on 2026-09-30's 35, and 29 affected, 28 up and 1 down on 2026-10-30's 46.
    The one leaning down is cme_ftp_daily.reported_change in both runs -- it
    is the only float column in the schema that holds negative values.
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

# The .5-boundary sentence that was withdrawn on 2026-10-26. Kept as a
# constant so the guard and its own mutation test below are looking for
# the identical string -- two copies of a banned phrase is how a ban
# quietly stops covering the thing it names.
CENSUS_CLAIM = "ZERO values sit within 1e-6 of a .5 boundary"
CAST_DOC = pv._int_cast.__doc__


def flat(s: str) -> str:
    """Whitespace-collapsed text, so a phrase still matches when the prose
    wraps it across two lines. Without this the tests would be pinning the
    line breaks as much as the numbers."""
    return re.sub(r"\s+", " ", s)


FLAT_DOC = flat(DOC)
FLAT_CAST = flat(CAST_DOC)


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


def sqlite_pushed_int(conn, table, column):
    """What the REAL SQLite returns for the expression the push actually uses.

    Not modelled. push_verify builds CAST(ROUND(v * FLOAT_SCALE) AS INTEGER)
    for this side, so that is what is asked, in one query per column rather
    than one per value -- 1,084,781 values in about 1.7s, which is cheap
    enough that the check can cover the whole schema instead of a sample.
    """
    return conn.execute(
        f'SELECT "{column}", '
        f'       CAST(ROUND("{column}" * {pv.FLOAT_SCALE}) AS INTEGER) '
        f'FROM {table} WHERE "{column}" IS NOT NULL')


def test_the_two_engines_agree_on_every_float_value_in_the_database(
        conn, float_columns):
    """
    THE CHECK THE .5-BOUNDARY PARAGRAPH IS ACTUALLY ABOUT, asked directly.

    What replaced what, and why. This used to count values sitting within
    1e-6 of a .5 boundary and compare that count against a census typed into
    the docstring. Nearness to a boundary is a PROXY for the two engines
    disagreeing, and on 2026-10-26 the proxy failed in both directions at
    once:

      * too narrow -- fci_daily.same_day_avg_weight for that day scaled to
        8336562.5, a dead-exact half, so the census went red over a value both
        engines round identically. It is also LIVE DATA, today's row, still
        filling. A test that has to be re-typed whenever a head count happens
        to carry five factors of two is a test that gets deleted;

      * too wide -- swept against this SQLite, only ONE of the two million
        halves k+0.5 over k in 0..2e6 has a one-ULP-below neighbour the
        engines read differently, and it is k = 0. Everything else the old
        census would have flagged was never a hazard.

    So ask the question instead of its proxy: run the pushed expression on
    both sides for every float value and compare. That is the same method
    CLAUDE.md prescribes -- "comparing a number against an independently
    derived number" -- and it is insensitive to which harmless halves today's
    data happens to contain.

    SQLite is real and Snowflake is modelled, which is the one weakness here.
    half_away() is checked against the live engine (see its docstring) and
    this suite deliberately needs no credentials; a model that drifted from
    Snowflake would make this quiet rather than loud, which is why the model
    lives in one place with its verification date on it.
    """
    compared = 0
    bad = []
    for t, c in float_columns:
        for v, got in sqlite_pushed_int(conn, t, c):
            try:
                s = float(v) * pv.FLOAT_SCALE
            except (TypeError, ValueError):
                continue
            if not math.isfinite(s):
                continue
            compared += 1
            want = half_away(s)
            if got != want:
                bad.append((t, c, float(v), s, got, want))

    assert compared > 100_000, (
        f"only {compared} float values were compared; the schema holds far "
        f"more, so something filtered them out and this passed vacuously")
    assert not bad, (
        f"{len(bad)} value(s) round differently on the two engines under the "
        f"pushed expression, so the push will mismatch on them: {bad[:5]}")


def test_that_check_can_still_fail(conn):
    """
    THE MUTATION. The test above is a wall of agreement; a wall of agreement
    is indistinguishable from a comparison that stopped comparing.

    0.49999999999999994 is the real divergence and the one the docstring
    names. SQLite's ROUND adds 0.5 and truncates the magnitude, and that
    addition carries here because the sum lands on a representable midpoint
    just under 1.0; Snowflake, rounding half away from zero on a FLOAT, reads
    it as 0. Fed through the identical code path as a one-row table, the
    assertion above must go red.
    """
    mem = sqlite3.connect(":memory:")
    try:
        mem.execute("CREATE TABLE t (v REAL)")
        # Stored pre-scaled: the column value is this over FLOAT_SCALE, and
        # that quotient is not itself representable, so planting the quotient
        # would not reproduce the scaled float.
        mem.execute("INSERT INTO t VALUES (?)",
                    (0.49999999999999994 / pv.FLOAT_SCALE,))
        (v, got), = sqlite_pushed_int(mem, "t", "v")
        s = float(v) * pv.FLOAT_SCALE
        assert s == 0.49999999999999994, (
            f"the planted value did not scale back to the hazard: {s!r}")
        assert got != half_away(s), (
            "SQLite and the Snowflake model AGREE on the one value the "
            "docstring says they do not. Either SQLite's ROUND changed under "
            "us or half_away() is no longer modelling Snowflake -- in both "
            "cases the test above has stopped being able to fail, and the "
            "docstring paragraph needs re-deriving, not patching.")
    finally:
        mem.close()


def test_the_paragraph_does_not_claim_a_clean_boundary_census_again(
        conn, float_columns):
    """
    What the prose is allowed to say. The old sentence -- "ZERO values sit
    within 1e-6 of a .5 boundary" -- was a standing claim about data that
    moves daily, and it went false the first day a head count was divisible
    by 32.

    So two things are pinned, both re-derived rather than typed: the engines'
    verdict, which is stable, and an admission in the prose that the boundary
    population is not. The near-boundary count is reported in the failure
    message instead of asserted, which is the same treatment the row-level
    counts get for the same reason.
    """
    near = []
    for t, c in float_columns:
        for (v,) in conn.execute(
                f'SELECT "{c}" FROM {t} WHERE "{c}" IS NOT NULL'):
            try:
                s = float(v) * pv.FLOAT_SCALE
            except (TypeError, ValueError):
                continue
            if math.isfinite(s) and abs(s - math.floor(s) - 0.5) <= 1e-6:
                near.append((t, c, float(v), s))

    assert flat("ZERO divergences") in FLAT_DOC, (
        "the paragraph no longer states the engine-against-engine result, "
        "which is the only standing claim it is entitled to make")
    # The old sentence is still in the prose, because the paragraph explains
    # what it replaced and why -- so BANNING the string outright would fail on
    # correct text, the exact trap the order-dependence test below documents.
    # What is banned is the sentence standing as a CLAIM: it may appear only
    # as the thing being withdrawn, immediately after "used to say".
    hits = [i for i in range(len(FLAT_DOC))
            if FLAT_DOC.startswith(CENSUS_CLAIM, i)]
    assert len(hits) == 1, (
        f"the withdrawn boundary-count claim appears {len(hits)} times in the "
        f"prose; it is allowed once, as history")
    lead = FLAT_DOC[max(0, hits[0] - 60):hits[0]]
    assert "used to say" in lead, (
        f"the boundary-count claim is being stated as fact again rather than "
        f"as the withdrawn one -- it reads {lead!r}. {len(near)} value(s) sit "
        f"within 1e-6 of a .5 boundary right now, on live data: "
        f"{[(t, c, v) for t, c, v, _ in near][:5]}")
    assert flat("LIVE DATA") in FLAT_DOC, (
        "the warning that the boundary population moves has been dropped; "
        "without it the next reader re-pins it")


def test_that_prose_guard_can_fail():
    """
    THE MUTATION for the guard above, which is a string check and so the
    easiest kind to write unfalsifiably. Three checks in this project turned
    out to be incapable of failing; CLAUDE.md's rule is to prove otherwise.

    The same assertion, run against prose that asserts the withdrawn claim
    instead of withdrawing it, must reject.
    """
    restated = flat(
        "MEASURED EXPOSURE IN THE REAL DATA: ZERO values sit within 1e-6 of a "
        ".5 boundary, so the push is safe.")
    hits = [i for i in range(len(restated))
            if restated.startswith(CENSUS_CLAIM, i)]
    assert len(hits) == 1
    assert "used to say" not in restated[max(0, hits[0] - 60):hits[0]], (
        "the guard would pass on prose that re-asserts the withdrawn claim, "
        "so it is not checking anything")


def test_the_hazard_the_paragraph_names_is_the_one_that_exists(conn,
                                                               float_columns):
    """
    The docstring narrows the hazard from "near any half" to |scaled| < 1.
    That is a claim about this schema's RANGE, not about rounding, and it is
    the reason a live exact half can be waved through -- so it is re-derived
    here rather than trusted.

    If a float column ever does carry a value that tiny, this goes red and the
    paragraph's reasoning has to be re-read before the next push, even though
    the engines may still agree on it today.
    """
    smallest = None
    for t, c in float_columns:
        for (v,) in conn.execute(
                f'SELECT "{c}" FROM {t} '
                f'WHERE "{c}" IS NOT NULL AND "{c}" <> 0'):
            try:
                s = abs(float(v) * pv.FLOAT_SCALE)
            except (TypeError, ValueError):
                continue
            if math.isfinite(s) and (smallest is None or s < smallest[0]):
                smallest = (s, t, c, float(v))
    assert smallest is not None, "no non-zero float values at all"
    assert smallest[0] >= 1.0, (
        f"{smallest[1]}.{smallest[2]} = {smallest[3]!r} scales to "
        f"{smallest[0]!r}, inside the |scaled| < 1 band the docstring says "
        f"this schema stays out of")
    assert flat("|scaled| < 1") in FLAT_DOC



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
