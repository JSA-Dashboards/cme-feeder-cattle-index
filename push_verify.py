"""
Content verification for the SQLite -> Snowflake push.

WHY THIS EXISTS. On 2026-09-29, 388 of the 949 rows in Snowflake's fci_daily
disagreed with local SQLite. The values were WRONG, not missing: 2026-09-23 was
served as 337.05 against the correct 337.0638, and Snowflake's fci_daily even
disagreed with Snowflake's own mars_sales. A re-push fixed it and the root cause
is still unknown. Throughout, the push printed [OK] on every table, because its
only verification was

    SELECT COUNT(*) FROM <table>          -- compared against len(df)

and a row count is blind to every value in the table. "388 wrong rows, correct
row count" is exactly the shape a count cannot see. Production reads Snowflake,
clients trade on the number, so the failure that check misses is "the dashboard
serves a wrong index value and nothing goes red".

WHAT THIS DOES. For each table it computes a FINGERPRINT server-side on both
backends -- a handful of aggregates per column -- and compares them exactly.
Nothing is downloaded: the push already uploads every row once, and pulling
33k mars_sales rows and 72k cme_ftp_brackets rows back down to diff them would
roughly double the push, which sits on the index's path to publication.

The column set is the one the push itself uploads ({SQLite columns} INTERSECT
{DESC TABLE columns}) and each column's class comes from its DESC TABLE type.
There is NO per-table configuration anywhere in this file. That is deliberate
and it is the rule in CLAUDE.md: "CRITICAL vs OPTIONAL lives in
snowflake/02_migrate_data.py, not in the PowerShell -- adding a table must not
require a matching edit somewhere nobody looks." A table added to
CRITICAL_TABLES or OPTIONAL_TABLES tomorrow is fingerprinted with no edit here.

    float columns   SUM(CAST(ROUND(col * 10000) AS <int64>))   and COUNT(col)
    int columns     SUM(col)                                   and COUNT(col)
    text/date       SUM(LENGTH(CAST(col AS VARCHAR))),
                    COUNT(DISTINCT col),
                    MIN/MAX of the first 64 characters         and COUNT(col)
    every column    COUNT(col)                -- so none is ever unchecked
    every table     COUNT(*)

WHY THE FLOAT SUMS ARE INTEGER-SCALED -- AND NOT FOR THE REASON THIS PARAGRAPH
USED TO GIVE. It used to say a raw SUM of floats is order-dependent, that
Snowflake parallelises the aggregation across micro-partitions while SQLite
does not, and that the two would therefore differ in the last bits on every
run and the check would cry wolf forever. That came from the design brief and
was never checked. IT IS NOT WHAT EITHER ENGINE DOES. Measured live,
read-only, 2026-09-30, over all 35 float columns in the schema:

    SQLite's SUM did not move when the scan was reversed --
    SELECT SUM(x) FROM (SELECT c AS x FROM t ORDER BY rowid DESC) matched the
    forward sum on 35 of 35 columns, bit for bit.

    Snowflake's SUM did not move when the ADDITION WAS RE-ASSOCIATED --
    SELECT SUM(s) FROM (SELECT SUM(c) s FROM t GROUP BY MOD(ABS(HASH(c)), 97))
    matched the flat sum on 35 of 35. That is the probe that matters: repeating
    the same query only shows the result cache working, whereas summing 97
    partial sums genuinely changes the grouping the addition happens in, which
    is the thing order-dependence would show up in.

So a raw SUM would not cry wolf, and if this module's only argument for the
scaling were order-dependence, the scaling would be decoration. It is not.
Keep it for the reason below, which is measurable, systematic and reproduces on
demand -- and do not re-introduce the order-dependence story, because the next
person to test it will find it false and start distrusting the rest of this
file.

THE MEASURED REASON: CAST-TO-INTEGER IS A DIFFERENT OPERATION ON THE TWO
BACKENDS, so the scaled value has to be ROUNDED before it is cast. SQLite
truncates toward zero; Snowflake rounds half-away-from-zero:

    SQLite     CAST( 2.9 AS INTEGER)             ->  2
    Snowflake  CAST(TO_DOUBLE( 2.9) AS BIGINT)   ->  3
    SQLite     CAST(-2.9 AS INTEGER)             -> -2
    Snowflake  CAST(TO_DOUBLE(-2.9) AS BIGINT)   -> -3

The cleanest way to say what the outer ROUND does, and the form to re-measure
if this is ever doubted: ROUND IS A NO-OP ON SNOWFLAKE AND LOAD-BEARING ON
SQLITE. Adding it changed Snowflake's scaled sum on 0 of 35 columns -- its CAST
was already rounding -- and changed SQLite's on 21 of 35. The ROUND is there to
drag SQLite onto Snowflake's behaviour, not to meet it in the middle.

PROBE THAT WITH TO_DOUBLE, NOT A BARE LITERAL. A decimal literal in Snowflake
is NUMBER, not FLOAT -- SYSTEM$TYPEOF(782.93365 * 10000) is NUMBER(13,5) -- and
NUMBER arithmetic is exact, so a bare-literal probe measures a different engine
than the FLOAT columns this check actually reads. It really does mislead:
ROUND(782.93365 * 10000) is 7829337 as a literal and 7829336 as the stored
FLOAT.

WHAT ACTUALLY TRIPS IT IS NOT .5 AT ALL. A price stored to two decimals is
almost never an exact integer once multiplied by 10000 in binary -- it lands an
ULP to one side. cme_ftp_locations, Windsor, 2026-09-23, avg_price 340.28:

    340.28 * 10000  ->  3402799.9999999995
    SQLite     CAST(... AS INTEGER)  ->  3402799      <- a whole unit lost
    Snowflake  CAST(... AS BIGINT)   ->  3402800
    both, with the outer ROUND        ->  3402800

That is why the damage is broad rather than freakish, and it falls in two
regimes. A column of TWO-DECIMAL prices only diverges when the binary
representation happens to land under the integer -- a few percent of rows
(0.6% to 7.5% across the affected columns, measured 2026-09-30). A column
holding a COMPUTED value to full double precision diverges about HALF the
time, because the scaled value is then an arbitrary real and falls past a .5
as often as not:

    fci_daily.same_day_price         342 of 659 rows      51.9%
    fci_snapshots.fci_value          471 of 930 rows      50.6%
    fci_daily.same_day_avg_weight    328 of 659 rows      49.8%
    fci_daily.fci_value              472 of 955 rows      49.4%
      -- against, for contrast --
    cme_ftp_brackets.avg_price     2,710 of 72,641 rows    3.7%

Those four are the whole of the half-diverging regime, and they are the INDEX
ITSELF and its snapshots -- the columns anybody would look at first to decide
whether a push was good. The row counts grow with the tables and are quoted
to show the shape, not as constants to check against; the two regimes are the
durable part.

WHAT THE "SIMPLIFICATION" COSTS, AND WHICH WAY IT LEANS. Dropping the outer
ROUND to leave CAST(col * 10000 AS INT) breaks 21 of the 35 float columns
immediately. It does NOT always make Snowflake read higher, which this
paragraph used to claim: truncation moves toward zero, so the sign of the data
decides the sign of the error. Snowflake reads higher on 20 of the 21 and LOWER
on one -- cme_ftp_daily.reported_change, the only float column in the schema
that holds negative values (1,240 of them). Its diverging rows split 37
positive against 39 negative and the errors partly cancel, so the column's
whole scaled sum is off by just -2:

    reported_change = -3.01  ->  -30099.999999999996
    SQLite    -> -30099        Snowflake -> -30100        with ROUND, both -30100

A near-cancelling column is the worst case to debug, not the mildest: a
two-unit discrepancy on one column looks like noise rather than like the
systematic breakage it is. Anyone who "simplifies" this buys a permanent false
alarm on most of the schema and a confusing one on the rest.

And ROUND(col, 4) * 10000 is NOT a substitute either. It rounds in the wrong
place -- to four decimals of the UNSCALED value, then multiplies the residue
back up -- and it leaves the same 21 columns disagreeing. Scale first, round
once.

The scale itself, 1e-4, is finer than the index is quoted to (fci_value is
published to 4 decimals), and int64 headroom is not close to a concern: the
largest scaled sum anywhere in the schema is replacement_sales.avg_weight at
8.79e11, 9.5e-8 of the int64 limit -- the table would need about 10.5 million
times its current rows to overflow.

WHAT THIS CANNOT CATCH. Being honest about the gap is worth more than a claim
of completeness, so:

  * A PERMUTATION INSIDE ONE COLUMN IS INVISIBLE, AND THIS IS THE REAL HOLE.
    If two rows exchange a value -- 09-11 serving 09-14's price and vice
    versa -- every aggregate here agrees and the push prints [CONTENT OK] over
    two wrong published numbers. This is not an oversight to be patched: an
    aggregate over ONE column reads that column's multiset and nothing else,
    and a permutation maps the multiset to itself, so SUM, COUNT,
    COUNT(DISTINCT), MIN, MAX and every higher moment are identical BY
    CONSTRUCTION. No per-column term of any kind can close it, and there is no
    shared row identity to order by (Snowflake has no stable rowid, and
    micro-partition scan order is not a thing to depend on).

    The realistic member of this family is a whole column shifted by one row
    during a load, which is a permutation and therefore equally invisible. Two
    compensating errors in one column (+0.01 on one row, -0.01 on another)
    cancel the same way, though that one really is a crafted input.

    What WOULD close it is a cross-column term tying each value to something
    else in its own row, e.g. SUM(scaled(col) * partner). It is not here
    because it does not fit: measured across all 20 tables,
    replacement_sales.avg_weight x avg_price reaches 7.75e18 (84% of int64)
    and two of that table's columns overflow outright in SQLite while
    Snowflake, summing into NUMBER(38,0), would not -- so the check would fail
    on the local side of a perfectly good push, forever. A modular variant
    ((scaled % p) * (partner % p)) does fit, at 3.26e16 worst case, and is the
    open option; it needs MOD's sign semantics verified live on both backends
    first (cme_ftp_daily.reported_change has 1,240 negative rows) and gives no
    coverage to the two tables with fewer than two numeric columns.

    Everything OTHER than a permutation moves a count, a sum or a prefix: a
    stale or partial load, a lost or extra row, a value nulled or zeroed, a
    column read into the wrong field, a re-run against the wrong window, and
    all five faults reproduced from the 09-29 incident.
    tests/test_push_verify.py pins both the catches and the gap.

  * THE TEXT FINGERPRINT IS A FINGERPRINT, NOT A HASH, AND HAS A KNOWN
    RESIDUAL HOLE. Neither backend offers a hash function the other has
    (SQLite ships no MD5/SHA), so there is nothing to compare digests of.
    SUM(LENGTH) plus COUNT(DISTINCT) alone does not see a length-preserving,
    cardinality-preserving substitution: a negative control rewrote every 'a'
    to 'x' in mars_sales.location and both aggregates came back IDENTICAL on
    the two sides -- same distinct count, same character total -- so the check
    said SAME over a column in which every value had changed. (The absolute
    figures from that run are not quoted here on purpose: mars_sales grows
    daily, so a reader re-deriving them would find different numbers and
    could not tell a stale docstring from a real fault. What is reproducible
    is that the substitution moves NEITHER aggregate.) The 64-character
    MIN/MAX prefix closes that case and a first-character change; what
    survives is a substitution that alters neither length, cardinality, nor
    the lexical min/max prefix.

  * DIFFERENCES BELOW 5e-5 ON A FLOAT COLUMN ARE INVISIBLE BY DESIGN.
    fci_daily stores up to 12 decimals but the index is published to 4, so the
    scale is set where the product is.

  * IT ONLY RUNS AT PUSH TIME, AND HALF OF THE 09-29 HYPOTHESIS SPACE IS
    OUTSIDE THAT WINDOW. The fingerprint compares the two backends moments
    after the COMMIT and never again. The root cause of 09-29 is still
    unknown, so "the push wrote it wrong" and "something changed it between
    pushes" are both live, and only the FIRST is covered. A corruption that
    arises AFTER a good push -- a stray UPDATE, a partial replay, an older
    file re-run against the same table -- stays invisible until the next push,
    and the log line immediately before it will read [CONTENT OK]. THAT [OK]
    IS NOT EXONERATING: it means Snowflake matched SQLite at 08:0x, not that
    Snowflake is right now. Closing this needs a scheduled re-fingerprint
    between pushes, which does not exist yet.

    THE CONVERSE TRAP, FOR WHOEVER BUILDS THAT. Running this comparison at an
    arbitrary moment is NOT a corruption check, because between pushes the two
    backends are SUPPOSED to differ: the pipeline recomputes locally and the
    rows sit in SQLite until the next push carries them. Re-deriving these
    numbers by hand on 2026-09-30 at midday found fci_daily's three float
    columns disagreeing across the two backends, on exactly two rows --
    2026-09-29 and 2026-09-30, the two most recent index dates, recomputed
    locally that morning and not yet pushed. That is the system working. An
    out-of-band re-fingerprint has to account for unpushed local work or it
    will page somebody every weekday lunchtime, and an alert that is wrong
    every day is an alert that gets switched off before the day it is right.
    It is also why no paragraph in this file should claim the two backends
    "agree on every column" as a standing fact: that is true immediately
    after a clean push and routinely false an hour later.

  * THIS IS A SQLITE-VS-SNOWFLAKE CHECK, NOT AN INTERNAL-CONSISTENCY ONE. It
    catches the 09-29 incident only because local SQLite happened to be
    correct; had both sides been wrong together it would have printed
    [CONTENT OK] over the whole thing. It does NOT verify that Snowflake's
    fci_daily agrees with Snowflake's own mars_sales, which that incident also
    violated -- nothing here recomputes the index from its inputs on either
    backend.

  * ONE ULP BELOW A .5 BOUNDARY, ROUND CAN DIVERGE. Both engines round exact
    halves half-away-from-zero (2.5 -> 3, -2.5 -> -3 on both). The divergence
    is just below a half: SQLite's round() with no digit argument is
    (int64)(r + 0.5), and that ADDITION can round up on its own.
    0.49999999999999994 + 0.5 is exactly 1.0 as a double, so SQLite's
    ROUND(0.49999999999999994) is 1.0 where Snowflake's, on a FLOAT, is 0.0.

    MEASURED EXPOSURE IN THE REAL DATA, re-derived read-only 2026-10-05 across
    all 35 float columns: ZERO values sit within 1e-6 of a .5 boundary. The
    closest approach in the whole database is 9.08e-4, at fci_daily.fci_value
    = 262.21194990916575.

    THAT NUMBER USED TO BE THREE, AND WHY IT IS NOW ZERO MATTERS. On 2026-09-30
    this paragraph listed 878.00155, 764.26255 and 782.93365, all three in
    cme_ftp_brackets.avg_weight, and singled out the last (El Reno, 2026-05-13,
    750 lb bracket) as sitting ONE ULP BELOW a half -- exactly the shape the
    paragraph warns about. All three were artefacts. cme_ftp.py's token path
    was gluing two columns together, so "782.93 365..." parsed as the single
    value 782.93365; the five-decimal precision that put them on a rounding
    boundary was never in CME's file at all. The slice-first fix on 2026-10-05
    corrected the twelve post-2020 dates carrying it and the pathological
    floats went with them.

    Which is the useful lesson here: a value sitting improbably close to a
    rounding boundary was evidence of a PARSER bug, not of float arithmetic.
    The census was measuring corruption and reporting it as a numerical edge
    case. Fifty-two rows across 2020-09-21..09-30 still carry the glue -- those
    files have no header for the slice path to read -- but none of them land
    within 1e-6 of a boundary.

    So a one-unit mismatch on a single float column and nothing else should be
    investigated as arithmetic before corruption -- but that is a measurement
    of the data as it stands, not a property of the schema, and it has already
    changed once. Re-run it before leaning on it.

Placement: called from snowflake/02_migrate_data.py immediately after each
table's COMMIT. Kept here, standalone and importable, because a guard that can
only be exercised by running a real push against production is a guard nobody
tests. tests/test_push_verify.py drives it against known-bad fixtures.
"""
from decimal import Decimal
from typing import NamedTuple

# Scale applied to float columns before summing. 1e-4 matches the index's
# published precision; see the module docstring for the headroom analysis.
FLOAT_SCALE = 10000

SQLITE = "sqlite"
SNOWFLAKE = "snowflake"

# The label used for the table-level COUNT(*), in place of a column name.
ROWS = "*"

_FLOAT_TYPES = ("FLOAT", "DOUBLE", "REAL")
_NUMERIC_TYPES = ("NUMBER", "DECIMAL", "NUMERIC", "INT", "BIGINT",
                  "SMALLINT", "TINYINT", "BYTEINT")
_TEXT_TYPES = ("VARCHAR", "TEXT", "STRING", "CHAR",
               "DATE", "TIMESTAMP", "TIME", "BOOLEAN")


class Column(NamedTuple):
    """One column as it is actually pushed, with the class its aggregates
    come from. `name` is the canonical uppercase name shared by both sides;
    `sqlite_name` is the local spelling, quoted into the SQLite SQL."""
    name: str
    sqlite_name: str
    sf_type: str
    cls: str            # "float" | "int" | "text" | "date" | "other"


class Difference(NamedTuple):
    table: str
    column: str         # a column name, or ROWS for the table-level count
    aggregate: str
    snowflake: object
    sqlite: object


def classify(sf_type: str) -> str:
    """
    A column's fingerprint class, FROM ITS SNOWFLAKE DECLARED TYPE and never
    from its name.

    That is not pedantry. mars_census.report_date, .raw_date and .index_date
    are VARCHAR on both sides while fci_daily.report_date is TEXT -> DATE, so
    a name-based "this looks like a date" rule picks the wrong expression for
    three columns on day one.

    Anything unrecognised returns "other", which still gets COUNT(col) -- a
    column excludes itself from value checking by its TYPE, never by being
    named in a list somewhere, and it is reported out loud rather than
    silently skipped.
    """
    t = (sf_type or "").upper().strip()
    if t.startswith(_FLOAT_TYPES):
        return "float"
    if t.startswith(_NUMERIC_TYPES):
        # NUMBER(p,s): a scale > 0 carries decimals, so treat it as a float.
        # Nothing in the schema does today; a future one must not be summed raw.
        if "(" in t and "," in t:
            try:
                scale = int(t.split(",")[1].rstrip(") ").strip())
            except ValueError:
                return "other"
            return "float" if scale > 0 else "int"
        return "int"
    if t.startswith("DATE"):
        # Pinned separately from the rest of the text class -- see _text_expr.
        return "date"
    if t.startswith(_TEXT_TYPES):
        return "text"
    return "other"


class Schemas(NamedTuple):
    """
    The three ways a column can relate to the two schemas. All three are
    derived, none is configured, and all three are reported.
    """
    columns: list           # on BOTH sides -- uploaded, and fingerprinted
    dropped: list           # SQLite only  -- uploaded by nothing, checked by nothing
    snowflake_only: list    # Snowflake only -- NULL on every row after the reload


def column_sets(sqlite_conn, sf_conn, table) -> Schemas:
    """
    How the two schemas line up for `table`.

    Derived exactly the way 02_migrate_data.py derives them (its DESC TABLE /
    `extra` block): the SQLite columns, uppercased, intersected with DESC
    TABLE. Reusing the same rule rather than inventing a second, subtly
    different one is the whole point -- if the two ever disagreed, the
    fingerprint would be checking a different set of columns than the push
    wrote.

    THE THIRD SET WAS MISSING AND THE DOCSTRING USED TO CLAIM OTHERWISE. This
    function computed only {SQLite} - {Snowflake} and then asserted that "there
    are no Snowflake-only columns anywhere. Neither fact is hardcoded; both are
    rediscovered here on every run." The second sentence was false:
    set(target) - set(local) was never computed, so a Snowflake-only column
    could not have been rediscovered by anything here, and the claim would have
    stayed comforting right through the day one appeared.

    It matters because of HOW the push writes. It DELETEs every row and reloads
    from a DataFrame that carries only the intersection, so a column present
    only in Snowflake ends up NULL on every row in production while the check
    prints [CONTENT OK] over the table. There is no SQLite side to compare such
    a column against, so it is REPORTED, the way a dropped column already is --
    and verify_table additionally checks the one thing that must be true of it
    after a successful push (see snowflake_only_values).

    Today exactly one column in the schema is dropped (mars_sales.
    published_date, which Snowflake lacks because ALTER there needs MODIFY and
    SYSADMIN was not granted it) and there are no Snowflake-only columns.
    Neither is hardcoded; both really are rediscovered on every run now.
    """
    # Both accessed exactly as 02_migrate_data.py accesses them -- iterating
    # the cursor execute() returns, not fetchall() -- so the two cannot drift
    # apart over something as silly as a cursor API difference.
    local = [(r[1], r[1].upper()) for r in
             sqlite_conn.execute(f"PRAGMA table_info({table})")]
    target = {r[0].upper(): r[1] for r in
              sf_conn.cursor().execute(f"DESC TABLE {table}")}

    columns, dropped = [], []
    for sqlite_name, upper in local:
        if upper not in target:
            dropped.append(upper)
            continue
        sf_type = target[upper]
        columns.append(Column(upper, sqlite_name, sf_type, classify(sf_type)))

    # The set the docstring used to promise. DESC TABLE order, not sorted, so
    # the report reads the way the table does.
    seen = {upper for _, upper in local}
    snowflake_only = [name for name in target if name not in seen]
    return Schemas(columns, dropped, snowflake_only)


def pushed_columns(sqlite_conn, sf_conn, table):
    """
    (columns, dropped) -- the two-value form of column_sets(), which is the
    shape callers unpack. Kept as its own name rather than widening the tuple,
    because a third return value is a silent breakage for anyone doing
    `cols, dropped = pushed_columns(...)`.
    """
    schemas = column_sets(sqlite_conn, sf_conn, table)
    return schemas.columns, schemas.dropped


def _ref(col: Column, dialect: str) -> str:
    """How this column is named on one side."""
    if dialect == SNOWFLAKE:
        return col.name
    # Quoted, so a column called "index" or "order" still parses. SQLite
    # identifier matching is case-insensitive, but the local spelling is used
    # anyway rather than relying on that.
    return '"' + col.sqlite_name.replace('"', '""') + '"'


def _int_cast(expr: str, dialect: str) -> str:
    """
    Scale-then-round-then-cast, which is the only form that agrees on both
    backends.

    DO NOT SIMPLIFY THE ROUND AWAY. SQLite's CAST(x AS INTEGER) truncates
    toward zero; Snowflake's CAST(x AS BIGINT) rounds half-away-from-zero.
    CAST(2.9) is 2 here and 3 there, CAST(-2.9) is -2 here and -3 there --
    measured on a FLOAT on both sides, because a bare decimal literal in
    Snowflake is NUMBER and rounds exactly, which makes a literal probe answer
    a question nobody asked.

    So the ROUND is a NO-OP on Snowflake (measured: it moves 0 of the 35 float
    columns' sums) and LOAD-BEARING on SQLite (it moves 21 of the 35). It is
    here to pull SQLite onto Snowflake's behaviour.

    The trigger is not .5 values, which are almost nonexistent in this data --
    it is that a two-decimal price times 10000 lands an ULP off an integer.
    340.28 * 10000 is 3402799.9999999995, which truncates to 3402799 here and
    casts to 3402800 there. Truncation moves toward ZERO, so the error's sign
    follows the data's: Snowflake reads higher on 20 of the 21 columns and
    LOWER on cme_ftp_daily.reported_change, the one column holding negatives.

    ROUND(col, 4) * 10000 is NOT the same thing -- it rounds the unscaled
    value and leaves the same 21 columns disagreeing. Scale first, round once.
    The module docstring has the full derivation and the re-measurement
    recipe; tests/test_push_verify_claims.py re-derives these counts from the
    live database and fails if this docstring drifts from them.
    """
    target = "BIGINT" if dialect == SNOWFLAKE else "INTEGER"
    return f"CAST(ROUND({expr}) AS {target})"


def _text_expr(col: Column, dialect: str) -> str:
    """
    The column rendered as a string, identically on both sides.

    A SQLite TEXT date and a Snowflake DATE both render as 'YYYY-MM-DD' under a
    plain CAST, so one expression would cover all 115 text-class columns. The
    catch is that CAST of a DATE honours the session's DATE_OUTPUT_FORMAT: if
    that is ever changed account-wide, all 33 DATE columns' length sums shift
    at once and the check screams about an index that is perfectly fine. This
    codebase already worries about exactly that ("calling this a failure would
    train someone to ignore a real one"), so DATE columns get an explicitly
    pinned format instead. The branch is on the DESC TABLE type, so it is still
    schema-driven -- no per-table config, no name matching.

    TIMESTAMP/TIME columns, of which the schema has none today, fall through to
    the plain CAST and would be exposed to TIMESTAMP_*_OUTPUT_FORMAT the same
    way; pin them here if one is ever added.
    """
    ref = _ref(col, dialect)
    if col.cls == "date" and dialect == SNOWFLAKE:
        return f"TO_CHAR({ref}, 'YYYY-MM-DD')"
    return f"CAST({ref} AS VARCHAR)"


def column_aggregates(col: Column, dialect: str):
    """(aggregate name, SQL) for one column on one side."""
    ref = _ref(col, dialect)

    # EVERY column, whatever its class. An unrecognised type then degrades to
    # "row presence checked, values not" rather than to nothing at all.
    out = [("count", f"COUNT({ref})")]

    if col.cls == "float":
        out.append(("scaled-sum",
                    f"SUM({_int_cast(f'{ref} * {FLOAT_SCALE}', dialect)})"))
    elif col.cls == "int":
        out.append(("sum", f"SUM({ref})"))
    elif col.cls in ("text", "date"):
        text = _text_expr(col, dialect)
        out.append(("len-sum", f"SUM(LENGTH({text}))"))
        out.append(("distinct", f"COUNT(DISTINCT {ref})"))
        # SUM(LENGTH) + COUNT(DISTINCT) provably misses a length-preserving,
        # cardinality-preserving substitution -- a negative control rewrote
        # every 'a' to 'x' in mars_sales.location and the check said SAME. The
        # prefix extremes catch that and a first-character change. Capped at 64
        # so a multi-KB border_reports.narrative never crosses the wire.
        out.append(("min64", f"MIN(SUBSTR({text}, 1, 64))"))
        out.append(("max64", f"MAX(SUBSTR({text}, 1, 64))"))
    return out


def fingerprint_sql(table: str, columns, dialect: str):
    """
    (sql, labels) for one side's whole fingerprint: a single SELECT.

    One statement per table rather than one per aggregate -- the widest table
    (hay_bids, 81 aggregates) still costs about half a second server-side.
    """
    if dialect not in (SQLITE, SNOWFLAKE):
        raise ValueError(f"unknown dialect {dialect!r}")
    labels = [(ROWS, "rows")]
    parts = ["COUNT(*)"]
    for col in columns:
        for agg, sql in column_aggregates(col, dialect):
            labels.append((col.name, agg))
            parts.append(sql)
    return f"SELECT {', '.join(parts)} FROM {table}", labels


def _normalize(v):
    """
    Decimal -> int so the two sides are comparable as plain Python values.

    The connector returns int for COUNT and for SUM over NUMBER(38,0) today,
    but that depends on the arrow_number_to_decimal setting rather than being
    a guarantee, so normalize defensively. A non-integral Decimal (which none
    of these expressions can produce) becomes a float rather than being
    silently truncated.
    """
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    return v


def fingerprint(conn, table: str, columns, dialect: str) -> dict:
    """
    {(column, aggregate): value} for one table on one backend, computed
    server-side in a single round trip.

    `columns` is a list of Column, normally from pushed_columns() so that both
    sides are fingerprinting the same column set.
    """
    sql, labels = fingerprint_sql(table, columns, dialect)
    cur = conn.cursor()
    cur.execute(sql)
    row = cur.fetchone()
    if row is None:                     # no aggregate query can do this
        raise RuntimeError(f"fingerprint of {table} returned no row: {sql}")
    return {label: _normalize(v) for label, v in zip(labels, row)}


def compare(sqlite_fp: dict, snowflake_fp: dict, table: str = "") -> list:
    """
    Every aggregate that differs, compared EXACTLY -- no tolerance. The
    integer scaling is what makes that safe; see the module docstring.

    A label present on one side only is reported too, with the absent side as
    the string "<absent>". That is how a dropped column surfaces rather than
    quietly falling out of the comparison.
    """
    out = []
    for label in sorted(set(sqlite_fp) | set(snowflake_fp)):
        column, agg = label
        lite = sqlite_fp.get(label, "<absent>")
        snow = snowflake_fp.get(label, "<absent>")
        if lite != snow:
            out.append(Difference(table, column, agg, snow, lite))
    return out


def describe(differences) -> list:
    """
    One human line per difference, naming the table, the column, the aggregate
    and BOTH values. "mismatch" is not actionable at 08:00; this is:

        fci_daily.FCI_VALUE scaled-sum: snowflake=2966923536 sqlite=2966923734 (diff -198)
    """
    lines = []
    for d in differences:
        where = f"{d.table}.{d.column}" if d.table else str(d.column)
        if d.column == ROWS:
            where = f"{d.table} row count" if d.table else "row count"
        line = f"{where} {d.aggregate}: snowflake={d.snowflake!r} sqlite={d.sqlite!r}"
        if isinstance(d.snowflake, (int, float)) and isinstance(d.sqlite, (int, float)) \
                and not isinstance(d.snowflake, bool) and not isinstance(d.sqlite, bool):
            line += f" (diff {d.snowflake - d.sqlite:+})"
        lines.append(line)
    return lines


class Verdict(NamedTuple):
    """
    The outcome for one table.

    `status` is one of:
        OK            -- N rows, at least one shared column, every aggregate
                         agreed
        MISMATCH      -- the contents differ; `differences` says how
        EMPTY         -- 0 rows on BOTH sides. NOT a pass: every aggregate is
                         NULL == NULL and nothing was actually verified.
        NO COLUMNS    -- rows on both sides, but the two schemas share no
                         column, so the only thing compared was COUNT(*).
                         0 rows is correctly EMPTY; 0 COLUMNS used to return
                         OK / verified / "1 aggregates" on a table whose every
                         value could have been wrong.
        INCONCLUSIVE  -- the check itself failed (see `error`). The push
                         succeeded; this says nothing about the contents.

    OK and INCONCLUSIVE must never print the same way, or the guard quietly
    degrades back into the COUNT check it replaced. `verified` is the single
    property that separates "we looked and it was right" from all four of the
    others, and 02_migrate_data.py branches on it -- before that it had no
    caller anywhere outside the tests, which is how EMPTY on a CRITICAL table
    exited 0.
    """
    table: str
    status: str
    rows: int
    n_aggregates: int
    differences: list
    dropped: list
    unclassified: list
    error: object = None
    # Appended after `error`, with a default, so every existing positional
    # construction of a Verdict keeps working unchanged.
    snowflake_only: tuple = ()

    @property
    def verified(self) -> bool:
        """
        True only when values were actually compared and agreed. Everything
        else -- empty, no shared columns, mismatched, or the check itself
        failing -- is False, so a status added later is not-verified by
        default rather than a silent pass.
        """
        return self.status == "OK" and self.rows > 0

    def summary(self) -> str:
        """The one-line content verdict appended to the push's own log line."""
        if self.status == "OK":
            return (f"[CONTENT OK] {self.n_aggregates} aggregates over "
                    f"{self.rows} row{'' if self.rows == 1 else 's'}")
        if self.status == "EMPTY":
            return "[CONTENT EMPTY] 0 rows both sides - nothing was verified"
        if self.status == "NO COLUMNS":
            return (f"[CONTENT NO COLUMNS] {self.rows} rows, but the two "
                    f"schemas share no column - only COUNT(*) was compared")
        if self.status == "MISMATCH":
            return f"[CONTENT MISMATCH] {len(self.differences)} aggregate(s) differ"
        return (f"[CONTENT INCONCLUSIVE] {type(self.error).__name__}: "
                f"{self.error}")


def snowflake_only_values(sf_conn, table: str, names) -> list:
    """
    The one thing that MUST be true of a Snowflake-only column, checked rather
    than assumed.

    Such a column cannot be fingerprinted -- there is no SQLite side to
    compare it against. But the push DELETEs every row of the table and then
    reloads it from a DataFrame that does not carry the column, so after a
    successful push it is NULL on every row and COUNT(col) is 0. That is a
    real, falsifiable claim about the pushed state, and it is worth making:
    a NON-ZERO count means rows in this table came from somewhere other than
    this push. The 2026-09-29 incident -- Snowflake's fci_daily disagreeing
    with both local SQLite and Snowflake's own mars_sales -- still has no root
    cause, and "something else is writing here" is a live hypothesis this is
    the only check in the system able to see.

    One extra round trip, and only when there is a Snowflake-only column at
    all; there are none today, so it is inert until the schemas diverge.

    Returns a list of Difference, empty when everything is NULL as it must be.
    """
    if not names:
        return []
    cur = sf_conn.cursor()
    cur.execute(f"SELECT {', '.join(f'COUNT({n})' for n in names)} FROM {table}")
    row = cur.fetchone() or ()
    return [Difference(table, name, "snowflake-only non-null count",
                       _normalize(v), 0)
            for name, v in zip(names, row) if _normalize(v)]


def verify_table(sqlite_conn, sf_conn, table: str) -> Verdict:
    """
    Fingerprint `table` on both backends and compare.

    NEVER RAISES. A Snowflake hiccup or an unexpected type must not turn a
    SUCCESSFUL push into a failure -- the index is already committed at this
    point and stranding it over a broken check would be strictly worse than
    the COUNT-only status this replaces. barn_report and mars_census take the
    same shape. The cost of that is that "inconclusive" exists, which is why
    it prints differently from "verified".
    """
    try:
        schemas = column_sets(sqlite_conn, sf_conn, table)
        columns = schemas.columns
        unclassified = [c.name for c in columns if c.cls == "other"]
        lite = fingerprint(sqlite_conn, table, columns, SQLITE)
        snow = fingerprint(sf_conn, table, columns, SNOWFLAKE)
        stowaways = snowflake_only_values(sf_conn, table, schemas.snowflake_only)
    except Exception as e:              # noqa: BLE001 -- see the docstring
        return Verdict(table, "INCONCLUSIVE", -1, 0, [], [], [], e)

    differences = compare(lite, snow, table) + stowaways
    rows = lite.get((ROWS, "rows"), 0) or 0
    n = len(lite)
    if differences:
        status = "MISMATCH"
    elif not columns:
        # Checked BEFORE the row test, because "the two schemas share no
        # column" is the more actionable statement: an empty table can be
        # legitimate for an OPTIONAL feed with nothing yet to report, whereas
        # a table the push cannot line up at all is broken under any
        # circumstances. (Which of the two is fatal is 02_migrate_data.py's
        # call, not this module's: it exits 3 on either for a CRITICAL
        # table.) Either way it is not
        # a pass -- this branch used to fall through to OK and report
        # "1 aggregates" over a COUNT(*) on a table whose every value could
        # have been wrong.
        status = "NO COLUMNS"
    elif rows == 0 and snow.get((ROWS, "rows"), 0) in (0, None):
        status = "EMPTY"
    else:
        status = "OK"
    return Verdict(table, status, rows, n, differences, schemas.dropped,
                   unclassified, None, tuple(schemas.snowflake_only))
