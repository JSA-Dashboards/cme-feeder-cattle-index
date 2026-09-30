"""
The WIRING around the content check, driven for real.

tests/test_push_verify.py proves that push_verify.py can tell a good table
from a corrupted one. This file proves that snowflake/02_migrate_data.py
ACTS on what it is told -- which is a separate claim, and the one that was
not covered.

WHY IT IS A SEPARATE FILE AND WHY IT LOOKS LIKE THIS. Six one-line edits to
02_migrate_data.py left the whole existing suite green, and three of them made
the push print a per-column mismatch in full and then exit 0. Every test
covering the wiring was an AST walk or a substring search over the source, and
those prove the code LOOKS right; they cannot prove it BEHAVES right. CLAUDE.md
already records three guards of exactly that class and the rule that follows
from them: "If you add a guard, prove it fails on bad input before trusting
it."

So nothing here reads the source. Every test runs the REAL main() end to end
against a stand-in Snowflake and asserts on the PROCESS EXIT CODE and on the
text the push actually printed. Each one was proved by applying the one-line
edit it pins, watching it go red, and reverting -- the defect each kills is
named in its docstring.

NOTHING HERE TOUCHES SNOWFLAKE OR data/mars_history.db. main() DELETEs every
table it is handed, so it is pointed at two temp SQLite files and
snowflake_db.get_conn is replaced BEFORE main() runs.

The three groups, and the holes they close:

  R1  NOTHING BOUNDED THE PUSH. get_conn() passes login_timeout=30 and
      nothing else; the connector defaults network_timeout to None ("If not
      specified, network_timeout is infinite" -- connection.py:589) and
      STATEMENT_TIMEOUT_IN_SECONDS is 172800 on both the session and the
      warehouse. daily_update.ps1 runs every step under `Start-Process -Wait`
      with no timeout, so a statement that never returns strands a FINISHED
      index unpublished. That happened on 2026-09-10. bound_session() existed
      and was never called, and PUSH_BUDGET_SECONDS existed and was never
      compared against anything.

  R2  Verdict.verified HAD NO CONSUMER. main() branched on MISMATCH and
      INCONCLUSIVE only, so EMPTY and NO COLUMNS -- the two verdicts that mean
      "the check ran and confirmed nothing" -- exited 0 looking exactly like a
      pass.

  R3  THE TWO COLUMN SETS NOBODY COMPARES were not reported. A Snowflake-only
      column is NULL on every row after the push and had no line in the log;
      verdict.dropped had no reader at all, so deleting the field broke no
      test.
"""
import contextlib
import importlib.util
import io
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import push_verify as pv                                    # noqa: E402

MIGRATE = REPO / "snowflake" / "02_migrate_data.py"

DDL = """
CREATE TABLE {name} (
    report_date  TEXT,
    fci_value    REAL,
    n_locations  INTEGER,
    total_head   INTEGER,
    location     TEXT
)
"""

ROWS = [
    ("2026-09-11", 337.0638, 19, 17_250, "APACHE (South Central)"),
    ("2026-09-14", 341.7073, 21, 18_004, "BASSETT"),
    ("2026-09-15", 342.7373, 23, 19_110, "APACHE (South Central)"),
]

# DESC TABLE's answer for the five real columns, types exactly as Snowflake
# reports them. classify() reads these and never a column name.
SF_TYPES = {
    "REPORT_DATE": "DATE",
    "FCI_VALUE": "FLOAT",
    "N_LOCATIONS": "NUMBER(38,0)",
    "TOTAL_HEAD": "NUMBER(38,0)",
    "LOCATION": "VARCHAR(16777216)",
}


def _to_char(value, fmt):
    """Snowflake's TO_CHAR(<DATE>, 'YYYY-MM-DD') for the fingerprint's DATE
    branch. Only the one pinned format is modelled; anything else raises
    rather than silently answering, because a stub that renders every format
    the same way cannot defend the format that makes the two sides
    comparable."""
    if value is None:
        return None
    if fmt != "YYYY-MM-DD":
        raise ValueError(f"TO_CHAR format {fmt!r} is not modelled by this stub")
    return str(value)[:10]


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
    A temp SQLite file wearing the Snowflake connection's clothes: DESC TABLE,
    ALTER SESSION, BEGIN/DELETE/COMMIT/ROLLBACK, the cheap SELECT COUNT(*),
    the fingerprint, and TO_CHAR.

    It also models the two things bound_session() reaches for on a real
    connection, because those are the subject of half this file:

      * `network_timeout` is a READ-ONLY property over `_network_timeout`,
        which is what snowflake.connector 4.7.3 does (connection.py:943, no
        setter) and why the push assigns the private name and then reads the
        public one back.
      * `readonly_network_timeout=True` makes even the private name
        unsettable, standing in for a connector upgrade that renamed it. The
        push must then say RETRIES UNBOUNDED rather than silently leaving the
        infinite retry loop in place.
    """

    def __init__(self, path, types=None, alter_boom=None,
                 fingerprint_boom=None, readonly_network_timeout=False):
        self.conn = sqlite3.connect(str(path), isolation_level=None)
        self.conn.create_function("TO_CHAR", 2, _to_char)
        self.types = dict(types or SF_TYPES)
        self.alter_boom = alter_boom
        self.fingerprint_boom = fingerprint_boom
        self.readonly_network_timeout = readonly_network_timeout
        self.statements = []
        self.closed = False
        object.__setattr__(self, "_nt", None)

    # --- the connector's timeout surface, modelled ---
    @property
    def network_timeout(self):
        return int(self._nt) if self._nt is not None else None

    @property
    def _network_timeout(self):
        return self._nt

    @_network_timeout.setter
    def _network_timeout(self, v):
        if self.readonly_network_timeout:
            raise AttributeError("_network_timeout is not settable here")
        object.__setattr__(self, "_nt", v)

    @staticmethod
    def _is_fingerprint(sql):
        head = sql.split(" FROM ", 1)[0]
        return head.upper().lstrip().startswith("SELECT") and "," in head

    class _Cur:
        def __init__(self, outer):
            self.outer = outer
            self.rows = []

        def execute(self, sql):
            self.outer.statements.append(sql)
            up = sql.upper().lstrip()
            if up.startswith("ALTER SESSION"):
                if self.outer.alter_boom:
                    raise self.outer.alter_boom
                self.rows = [("Statement executed successfully.",)]
                return self.rows
            if up.startswith("DESC TABLE"):
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


class Clock:
    """A monotonic clock whose readings are scripted, so a budget overrun can
    be reached without the test taking fifteen minutes. The last value
    repeats."""

    def __init__(self, values):
        self.values = list(values)
        self.reads = 0

    def monotonic(self):
        v = self.values[min(self.reads, len(self.values) - 1)]
        self.reads += 1
        return v


def _make_db(path, tables, extra_cols=None):
    """A SQLite file holding {table: rows}, every table on the same DDL, plus
    any per-table extra columns (used to give one side a column the other
    lacks)."""
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        for name, rows in tables.items():
            conn.execute(DDL.format(name=name))
            for col, decl in (extra_cols or {}).get(name, []):
                conn.execute(f"ALTER TABLE {name} ADD COLUMN {col} {decl}")
            if rows:
                conn.executemany(
                    f"INSERT INTO {name} (report_date, fci_value, n_locations, "
                    f"total_head, location) VALUES (?,?,?,?,?)", rows)
    finally:
        conn.close()


class PushRun:
    def __init__(self, code, out, stub, mod, error=None):
        self.code = code
        self.out = out
        self.stub = stub
        self.mod = mod
        # The exception main() let escape, if any. An escaping exception IS a
        # non-zero exit for the real process (`python 02_migrate_data.py`
        # exits 1 on a traceback), so it is recorded as code 1 rather than
        # swallowed -- but it is kept here so a test can tell a deliberate
        # failure from a broken rig.
        self.error = error

    def touched(self, table):
        """Did the push issue ANY statement naming this table?"""
        return any(table in s for s in self.stub.statements)


def run_push(monkeypatch, tmp_path, *, critical=("fci_daily",), optional=(),
             rows=None, local_extra=None, sf_types=None, sf_extra_cols=None,
             corrupt=None, alter_boom=None, fingerprint_boom=None,
             readonly_network_timeout=False, clock=None, group=None):
    """
    Run the REAL snowflake/02_migrate_data.py main() against two temp SQLite
    files and return its exit code and output.

      rows            {table: [row, ...]} for the local side (default: ROWS)
      local_extra     {table: [(col, decl)]} columns SQLite has and the stub
                      does not -- the dropped-column case
      sf_types        DESC TABLE's answer, replacing SF_TYPES entirely
      sf_extra_cols   {table: [(col, decl)]} columns the STUB has and SQLite
                      does not -- the Snowflake-only case
      corrupt         {table: "SQL"} run against the stub after the reload
      clock           a Clock, substituted for the module's `time`
    """
    import snowflake.connector.pandas_tools as pandas_tools
    import snowflake_db

    tables = list(critical) + list(optional)
    rows = dict(rows or {}) if rows is not None else {}
    rows = {t: rows.get(t, ROWS) for t in tables}

    local_path = tmp_path / "local.db"
    stub_path = tmp_path / "stub.db"
    _make_db(local_path, rows, local_extra)
    _make_db(stub_path, {t: [] for t in tables}, sf_extra_cols)

    types = dict(sf_types) if sf_types is not None else dict(SF_TYPES)
    stub = StubSnowflake(stub_path, types=types, alter_boom=alter_boom,
                         fingerprint_boom=fingerprint_boom,
                         readonly_network_timeout=readonly_network_timeout)
    for _t, cols in (sf_extra_cols or {}).items():
        for col, decl in cols:
            stub.types[col.upper()] = ("FLOAT" if "REAL" in decl.upper()
                                       else "VARCHAR(16777216)")

    def fake_write_pandas(conn, df, table, **kwargs):
        name = table.lower()
        cols = list(df.columns)
        if cols:
            sql = (f"INSERT INTO {name} ({', '.join(cols)}) "
                   f"VALUES ({', '.join('?' * len(cols))})")
            conn.conn.executemany(
                sql, [tuple(_sqlite_value(v) for v in r)
                      for r in df.itertuples(index=False, name=None)])
        else:
            # The push filtered every column away, which is the NO COLUMNS
            # case. write_pandas would still land the rows.
            for _ in range(len(df)):
                conn.conn.execute(f"INSERT INTO {name} DEFAULT VALUES")
        stmt = (corrupt or {}).get(name)
        if stmt:
            conn.conn.execute(stmt)
        return True, 1, len(df), None

    monkeypatch.setenv("USE_SNOWFLAKE", "")
    monkeypatch.setattr(snowflake_db, "get_conn", lambda: stub)
    monkeypatch.setattr(pandas_tools, "write_pandas", fake_write_pandas)

    spec = importlib.util.spec_from_file_location("_migrate_robustness", MIGRATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "DB_PATH", local_path)
    monkeypatch.setattr(mod, "CRITICAL_TABLES", list(critical))
    monkeypatch.setattr(mod, "OPTIONAL_TABLES", list(optional))
    monkeypatch.setattr(mod, "TABLES", tables)
    if clock is not None:
        monkeypatch.setattr(mod, "time", clock)

    buf = io.StringIO()
    code, error = 0, None
    with contextlib.redirect_stdout(buf):
        try:
            mod.main(group=group)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
        except Exception as e:                          # noqa: BLE001
            # What `python snowflake/02_migrate_data.py` does with an
            # escaping exception: prints a traceback and exits 1.
            code, error = 1, e
    return PushRun(code, buf.getvalue(), stub, mod, error)


# ---------------------------------------------------------------------------
# THE CONTROL. Without a push that PASSES, every exit-3 assertion below could
# be passing because the rig is broken rather than because the guard works.
# ---------------------------------------------------------------------------

def test_the_rig_can_produce_a_clean_push(monkeypatch, tmp_path):
    run = run_push(monkeypatch, tmp_path)
    assert run.code == 0, run.out
    assert "[CONTENT OK] 17 aggregates over 3 rows" in run.out, run.out
    assert "confirmed NOTHING" not in run.out
    assert "present only in Snowflake" not in run.out
    assert "present only in SQLite" not in run.out


# ---------------------------------------------------------------------------
# R1 -- THE CLOCK.
# ---------------------------------------------------------------------------

def test_the_session_is_bounded_before_the_push_issues_any_other_statement(
        monkeypatch, tmp_path):
    """
    KILLS: deleting the `bound_session(sf_conn)` call in main().

    The function existed and nothing called it, which is worth exactly
    nothing. And WHERE it is called is half the value: the DELETE,
    write_pandas and SELECT COUNT(*) are all older than the content check and
    were never bounded by anything, so the ALTER SESSION has to be the FIRST
    statement on the connection for the bound to reach them. This asserts on
    the order the stub actually saw, not on the source.
    """
    run = run_push(monkeypatch, tmp_path)
    assert run.code == 0, run.out
    assert run.stub.statements, "the push issued no statements at all"
    first = run.stub.statements[0]
    assert first == (f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = "
                     f"{run.mod.STATEMENT_TIMEOUT_SECONDS}"), (
        f"the first statement on the push's connection was {first!r}. Every "
        f"statement issued before the bound is set is unbounded, which is the "
        f"2026-09-10 hang.")
    assert (f"session bounds: statements {run.mod.STATEMENT_TIMEOUT_SECONDS}s"
            in run.out), run.out


def test_the_statement_bound_is_actually_a_bound(monkeypatch, tmp_path):
    """
    KILLS: STATEMENT_TIMEOUT_SECONDS = 172800 (or 0, which Snowflake reads as
    the 604800 maximum).

    Setting the parameter to the value it already has is a no-op dressed as a
    fix, and the ALTER SESSION would still be the first statement, so the test
    above would stay green. The range comes from measurement, re-derived
    read-only on 2026-09-30: the slowest statement this push has ever issued
    in the 15 days QUERY_HISTORY retains is DELETE FROM border_reports at
    6.319s, and the slowest fingerprint is 1.031s.
    """
    run = run_push(monkeypatch, tmp_path)
    t = run.mod.STATEMENT_TIMEOUT_SECONDS
    assert 30 <= t <= 600, (
        f"STATEMENT_TIMEOUT_IN_SECONDS = {t}. Below 30s a legitimately slow "
        f"DELETE would be cancelled; above 600s this is not a bound -- the "
        f"push's slowest ever statement is 6.3s and daily_update.ps1 waits on "
        f"it with no timeout of its own.")
    assert run.mod.NETWORK_TIMEOUT_SECONDS > t, (
        "the client-side retry deadline must sit ABOVE the server-side "
        "statement timeout, so a long statement loses to the server's own "
        "cancellation and the push gets the explanatory error rather than a "
        "bare client-side give-up.")


def test_the_retry_loop_is_bounded_on_the_connection_the_push_uses(
        monkeypatch, tmp_path):
    """
    KILLS: deleting the `sf_conn._network_timeout = NETWORK_TIMEOUT_SECONDS`
    assignment.

    STATEMENT_TIMEOUT is enforced by the SERVER. If the response never
    arrives, the client waits -- and connector 4.7.3 reads
    `self._connection.network_timeout` fresh on every request (cursor.py:693,
    network.py:900) with None meaning infinite retries. This asserts the value
    is readable back through the PUBLIC property afterwards, which is the only
    thing that distinguishes "set" from "assigned to an attribute nothing
    reads".
    """
    run = run_push(monkeypatch, tmp_path)
    assert run.stub.network_timeout == run.mod.NETWORK_TIMEOUT_SECONDS, (
        f"after the push the connection's network_timeout is "
        f"{run.stub.network_timeout!r}; the retry loop is unbounded.")
    assert "RETRIES UNBOUNDED" not in run.out
    assert f"retries {run.mod.NETWORK_TIMEOUT_SECONDS}s" in run.out


def test_a_bound_that_could_not_be_set_says_so_instead_of_going_quiet(
        monkeypatch, tmp_path):
    """
    KILLS: swallowing either failure silently (`except: pass` on the ALTER
    SESSION, or dropping the read-back of network_timeout).

    Both halves can fail for reasons nobody controls -- a revoked ALTER
    SESSION privilege, a connector upgrade that renames the private
    attribute -- and the push must still run, because an unbounded push beats
    no push. What it must NOT do is claim a bound it does not have. The word
    WARNING is load-bearing: check_run.ps1's morning digest filters the log on
    'WARN', so a line without it is invisible.
    """
    run = run_push(monkeypatch, tmp_path,
                   alter_boom=RuntimeError("SQL access control error"),
                   readonly_network_timeout=True)
    assert run.code == 0, ("a push that cannot set a timeout must still push; "
                           f"it exited {run.code}\n{run.out}")
    assert "WARNING: session bounds:" in run.out, run.out
    assert "STATEMENTS UNBOUNDED (RuntimeError)" in run.out, run.out
    assert "RETRIES UNBOUNDED" in run.out, run.out
    # And the push really did go on to do its job.
    assert "[CONTENT OK] 17 aggregates over 3 rows" in run.out, run.out


def test_a_push_that_overruns_its_budget_stops_before_the_next_critical_table(
        monkeypatch, tmp_path):
    """
    KILLS: deleting the PUSH_BUDGET_SECONDS check at the top of the loop.

    The two session bounds cap ONE statement each. Twenty tables x nine
    statements x 120s is six hours, which is the 2026-09-10 shape all over
    again -- daily_update.ps1 waits on this with `Start-Process -Wait` and no
    timeout of its own, so the index is finished and unpublished the whole
    time.

    Exit 1 and not 3, and the distinction is the point: the second table was
    never opened, so nothing was DELETEd for it and daily_update.ps1's
    "Snowflake still holds its previous contents" is TRUE of it.
    """
    run = run_push(monkeypatch, tmp_path,
                   critical=("fci_daily", "mars_sales"),
                   clock=Clock([0, 0, 10_000]))

    assert run.code == 1, (
        f"the push overran its budget with a critical table still to go and "
        f"exited {run.code}\n{run.out}")
    assert run.touched("fci_daily"), "the first table should have been pushed"
    assert not run.touched("mars_sales"), (
        "the push kept going past its budget; mars_sales was touched")
    assert "past its" in run.out and "budget" in run.out, run.out
    assert "CRITICAL table(s) were never pushed: mars_sales" in run.out, run.out
    assert "STALE" in run.out, (
        "the message must say the untouched tables are stale, not wrong -- "
        "that is the whole difference between exit 1 and exit 3")


def test_a_budget_overrun_with_only_dashboard_tables_left_is_not_an_index_failure(
        monkeypatch, tmp_path):
    """
    THE OTHER DIRECTION, and it is why the budget branches on what is LEFT
    rather than just exiting.

    KILLS: making the overrun unconditionally `raise SystemExit(1)`.

    The index published fine; only a dashboard tab is late. Reporting that as
    a push failure is the same harm the CRITICAL/OPTIONAL split was built to
    avoid -- daily_update.ps1 would log "DASHBOARD IS STALE" and mail a
    failure about an index that committed perfectly. "Calling this a failure
    would train someone to ignore a real one."
    """
    run = run_push(monkeypatch, tmp_path,
                   critical=("fci_daily",), optional=("calf_sales",),
                   clock=Clock([0, 0, 10_000]))

    assert run.code == 0, (
        f"a dashboard table left unpushed by the budget exited {run.code}, "
        f"which daily_update.ps1 reads as an index failure\n{run.out}")
    assert run.touched("fci_daily")
    assert not run.touched("calf_sales")
    assert "calf_sales" in run.out
    assert "will be stale" in run.out, run.out


# ---------------------------------------------------------------------------
# R2 -- "THE CHECK RAN AND CONFIRMED NOTHING" IS NOT A PASS.
# ---------------------------------------------------------------------------

def test_an_empty_critical_table_fails_the_push_instead_of_passing_it(
        monkeypatch, tmp_path):
    """
    KILLS: the `elif not verdict.verified:` branch, and dropping
    critical_unconfirmed from the exit condition.

    THE WHOLE SHAPE OF THE FAILURE. If local fci_daily is ever emptied the
    push DELETEs Snowflake's copy, inserts nothing, the cheap count reads
    0 == 0 and prints [OK], and the fingerprint compares NULL against NULL on
    every aggregate and finds no difference. Before this, the run exited 0 and
    the dashboard served an empty index.

    Verdict.verified is False for EMPTY precisely so this case has a handle,
    and until now it had no consumer anywhere outside the tests.
    """
    run = run_push(monkeypatch, tmp_path, rows={"fci_daily": []})

    # First prove the rig really did reach the case -- a test that passes
    # because the table was not empty after all proves nothing.
    assert "fci_daily: sqlite=0 snowflake=0 [OK]" in run.out, run.out
    assert "[CONTENT EMPTY] 0 rows both sides" in run.out, run.out
    assert run.code == 3, (
        f"an empty CRITICAL table exited {run.code}. The write COMMITTED and "
        f"Snowflake now holds nothing.\n{run.out}")
    assert "fci_daily: CONTENT NOT CONFIRMED (CRITICAL)" in run.out, run.out
    assert "CRITICAL table(s) loaded but nothing about their contents was "\
           "confirmed: fci_daily" in run.out, run.out


def test_an_empty_optional_table_is_reported_but_does_not_fail_the_push(
        monkeypatch, tmp_path):
    """
    THE OTHER DIRECTION, and the reason emptiness is not simply fatal
    everywhere.

    KILLS: making `not verdict.verified` exit 3 regardless of scope.

    An empty OPTIONAL table is legitimate. mars_census and mars_census_runs
    are written by a census step deliberately allowed to fail without failing
    the day, and on a freshly built database several dashboard feeds are empty
    until their first ingest. Failing the index push over one would train
    somebody to ignore exit 3, which is the only signal the 2026-09-29 shape
    has.
    """
    run = run_push(monkeypatch, tmp_path,
                   critical=("fci_daily",), optional=("calf_sales",),
                   rows={"calf_sales": []})

    assert "calf_sales: sqlite=0 snowflake=0 [OK]" in run.out, run.out
    assert "[CONTENT EMPTY] 0 rows both sides" in run.out, run.out
    assert run.code == 0, (
        f"an empty DASHBOARD table failed the index push (exit {run.code})"
        f"\n{run.out}")
    # It is NOT silently dropped: the verdict line says in so many words
    # that nothing was verified, and it is the only line that does.
    assert "nothing was verified" in run.out, run.out
    # ...and it must not be mistaken for the index having a problem. Both
    # of these appear only for a CRITICAL table.
    assert "CONTENT NOT CONFIRMED" not in run.out, run.out
    assert "CRITICAL table(s) loaded but nothing" not in run.out, run.out


def test_a_critical_table_sharing_no_column_with_snowflake_is_not_a_pass(
        monkeypatch, tmp_path):
    """
    KILLS: the same `elif not verdict.verified:` branch, by the other route.

    0 rows is EMPTY; 0 COLUMNS is NO COLUMNS, and it is worse, because the
    rows are there and every value in them could be wrong. verify_table used
    to return OK / verified / "1 aggregates" here, the one aggregate being a
    COUNT(*) that the cheap check had already done.

    The rows land -- write_pandas still writes them -- so the count agrees and
    the only thing that can catch this is the verdict.
    """
    run = run_push(monkeypatch, tmp_path,
                   sf_types={"WIDGET_ID": "NUMBER(38,0)"},
                   sf_extra_cols={"fci_daily": [("widget_id", "INTEGER")]})

    assert "fci_daily: sqlite=3 snowflake=3 [OK]" in run.out, run.out
    assert "[CONTENT NO COLUMNS] 3 rows" in run.out, run.out
    assert run.code == 3, (
        f"a critical table whose schemas share no column exited {run.code} "
        f"while only COUNT(*) had been compared\n{run.out}")
    assert "fci_daily: CONTENT NOT CONFIRMED (CRITICAL)" in run.out, run.out


# ---------------------------------------------------------------------------
# R3 -- THE TWO COLUMN SETS NOBODY COMPARES, SAID OUT LOUD.
# ---------------------------------------------------------------------------

def test_a_snowflake_only_column_is_named_in_the_push_log(monkeypatch,
                                                          tmp_path):
    """
    KILLS: deleting the `if verdict.snowflake_only:` line at the call site.

    pushed_columns() reported only SQLite-side drops; set(target) - set(local)
    was never computed, while the docstring claimed both were "rediscovered
    here on every run". After DELETE + write_pandas a column that exists only
    in Snowflake is NULL on every row in production, and the push printed
    [CONTENT OK] straight over it. There is no local side to compare it
    against, so the honest answer is to REPORT it -- the way a dropped column
    already is -- rather than to keep claiming it was checked.
    """
    run = run_push(monkeypatch, tmp_path,
                   sf_extra_cols={"fci_daily": [("settlement_id", "TEXT")]})

    assert run.code == 0, run.out
    assert "present only in Snowflake" in run.out and "SETTLEMENT_ID" in run.out, (
        f"a Snowflake-only column passed unmentioned under a [CONTENT OK]"
        f"\n{run.out}")
    assert "NULL on every row after this push" in run.out, run.out


def test_a_snowflake_only_column_that_still_holds_values_fails_the_push(
        monkeypatch, tmp_path):
    """
    KILLS: snowflake_only_values() not being called, or its result not
    reaching verdict.differences.

    The one thing that MUST be true of such a column after a good push is that
    it is NULL on every row: the push DELETEs everything and reloads from a
    frame that does not carry it. A non-zero count means rows in this table
    came from somewhere other than this push -- which is a live hypothesis for
    2026-09-29, whose root cause is still unknown, and this is the only check
    in the system able to see it.
    """
    run = run_push(monkeypatch, tmp_path,
                   sf_extra_cols={"fci_daily": [("settlement_id", "TEXT")]},
                   corrupt={"fci_daily": "UPDATE fci_daily SET settlement_id = 'x'"})

    assert run.code == 3, (
        f"rows written by something other than this push exited {run.code}"
        f"\n{run.out}")
    assert "SETTLEMENT_ID snowflake-only non-null count: snowflake=3 sqlite=0" \
        in run.out, run.out
    assert "fci_daily: CONTENT VERIFICATION FAILED (CRITICAL)" in run.out


def test_a_sqlite_only_column_is_named_by_the_verdict_and_not_only_by_the_push(
        monkeypatch, tmp_path):
    """
    KILLS: deleting the `if verdict.dropped:` line at the call site.

    main() already prints its own "skipping column(s) absent in Snowflake"
    from its own DESC TABLE. That is not the same statement: it says what the
    UPLOAD dropped, and this says what the CHECK is not looking at. Until this
    line existed, removing `dropped` from the Verdict entirely broke no test,
    so the check could have quietly stopped tracking which columns it was
    blind to.

    The real one is mars_sales.published_date, which Snowflake lacks because
    ALTER there needs MODIFY and SYSADMIN was not granted it.
    """
    run = run_push(monkeypatch, tmp_path,
                   local_extra={"fci_daily": [("published_date", "TEXT")]})

    assert run.code == 0, run.out
    # The push's own line about the upload...
    assert "skipping column(s) absent in Snowflake: PUBLISHED_DATE" in run.out
    # ...and the verdict's separate line about the check.
    assert ("present only in SQLite, so not uploaded and NOT verified: "
            "PUBLISHED_DATE") in run.out, (
        f"the verdict's dropped-column list has no reader\n{run.out}")


def test_the_notes_stay_quiet_when_the_two_schemas_agree(monkeypatch,
                                                         tmp_path):
    """
    A note printed on every table on every run is a note nobody reads. Both
    sets are empty in the real schema except for mars_sales.published_date, so
    a clean table must print neither line -- which also proves the two tests
    above are reading a real signal and not a constant.
    """
    run = run_push(monkeypatch, tmp_path)
    assert "present only in Snowflake" not in run.out
    assert "present only in SQLite" not in run.out
    assert "skipping column(s)" not in run.out


# ---------------------------------------------------------------------------
# WHAT HAPPENS WHEN THE BOUND ACTUALLY FIRES. It must surface as a clean
# failure or as INCONCLUSIVE -- never as a hang, and never as a pass.
# ---------------------------------------------------------------------------

def test_a_statement_timeout_during_the_load_rolls_back_and_fails_cleanly(
        monkeypatch, tmp_path):
    """
    When STATEMENT_TIMEOUT fires, Snowflake CANCELS the statement and the
    connector raises. Inside the transaction that means ROLLBACK, so the table
    keeps its previous contents and the push exits non-zero -- stale, not
    wrong. Modelled by making the DELETE raise the error Snowflake actually
    sends.
    """
    timeout_error = RuntimeError(
        "000630 (57014): Statement reached its statement or warehouse timeout "
        "of 120 second(s) and was canceled.")

    real_execute = StubSnowflake._Cur.execute

    def execute(self, sql):
        if sql.upper().startswith("DELETE FROM"):
            self.outer.statements.append(sql)
            raise timeout_error
        return real_execute(self, sql)

    monkeypatch.setattr(StubSnowflake._Cur, "execute", execute)
    run = run_push(monkeypatch, tmp_path)

    assert run.code != 0, "a cancelled load exited 0"
    assert run.error is timeout_error, (
        f"the push swallowed the cancellation: {run.error!r}")
    assert "fci_daily: FAILED - rolled back, table left as it was" in run.out
    assert "ROLLBACK" in run.stub.statements


def test_a_statement_timeout_during_the_check_is_inconclusive_not_a_pass(
        monkeypatch, tmp_path):
    """
    The other side of the same bound. By the time the fingerprint runs the
    index is already COMMITTED and correct, so a cancelled CHECK must not turn
    a good push into a failure -- but it must not print like a pass either.
    INCONCLUSIVE, named, with the scope, and repeated in the run summary where
    check_run.ps1's digest will find the word WARNING.
    """
    run = run_push(monkeypatch, tmp_path, fingerprint_boom=RuntimeError(
        "000630 (57014): Statement reached its statement or warehouse timeout "
        "of 120 second(s) and was canceled."))

    assert run.code == 0, (
        f"a cancelled CHECK failed a push whose index committed fine "
        f"(exit {run.code})\n{run.out}")
    assert "[CONTENT INCONCLUSIVE] RuntimeError:" in run.out, run.out
    assert "fci_daily: WARNING - content verification could not run (CRITICAL)"\
        in run.out
    assert "WARNING: 1 CRITICAL table(s) loaded but their contents could NOT "\
           "be verified: fci_daily" in run.out, run.out
    assert "[CONTENT OK]" not in run.out, (
        "an inconclusive check printed like a passing one")
