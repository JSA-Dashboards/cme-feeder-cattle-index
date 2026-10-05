"""
The cash calf series must never reach the feeder cattle index.

WHY THIS IS A TEST AND NOT A COMMENT. calf_sales holds 400-900 lb steers; the
index is 700-899 lb only. recompute_fci_daily() reads mars_sales with NO WHERE
CLAUSE -- every row in that table is treated as index-qualifying, the filtering
having happened once on the way in. So a single misrouted insert would put 400 lb
calves at $475/cwt into the published index with no error and no warning, and
the first sign would be a number that disagreed with CME by several dollars.

The risk is not hypothetical. CALF_BRACKETS was widened to include 900 on
2026-09-15 to serve the Cash Feeder Prices lookup, which is exactly the shape of
change that could creep: one bracket set grows, someone later "tidies" two
similar-looking ingests into one, and the index quietly changes.

These tests are structural rather than numeric so they keep working as the data
moves. They run without a database; the one that wants it skips politely.
"""
import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# Everything that computes, stores or displays the index. barn_report.py is on
# this list because update_index.py imports it and it reads mars_sales: it only
# prints today, but it is inside the index process and one JOIN away from the
# same mistake as everything else here.
INDEX_MODULES = ["update_index.py", "app.py", "bucketing.py", "snapshots.py",
                 "composition.py", "volumes.py", "reporting.py", "notify_email.py",
                 "mars_census.py", "mars_census_view.py"]

# On the index path, but EXEMPT from the calf_sales ban because they cannot
# carry anything to the index: they return text, nothing consumes that text but
# a log line and a caption, and they write nothing at all. barn_report.py moved
# here on 2026-10-05 -- see the block at the foot of this file for what it has
# to keep proving to stay here. Do not add to this list casually; the ban is
# cheap and robust, and every exemption costs some of that.
DIAGNOSTIC_MODULES = ["barn_report.py"]

# The index's own weight brackets, per CME Rule 10203.A.1 (700-899 lb).
INDEX_BRACKETS = {700, 750, 800, 850}


# Actual data access, not mentions. The first version of this test banned the
# STRING "calf_sales" anywhere in an index module, and promptly failed on a
# comment explaining that the cash series cannot reach the index -- punishing
# the documentation while proving nothing about the code. These patterns are
# how a table is really read or written.
ACCESS = ["from calf_sales", "join calf_sales", "into calf_sales",
          "update calf_sales", "calf_sales where", "calf_sales group",
          '"calf_sales"', "'calf_sales'"]


@pytest.mark.parametrize("name", INDEX_MODULES)
def test_no_index_module_reads_calf_sales(name):
    """
    Nothing on the index path may query the cash table. A JOIN or a UNION added
    "just to enrich the display" is how these things start.

    Comments and prose about calf_sales are fine and wanted -- the isolation is
    worth explaining where someone might otherwise undo it.
    """
    f = REPO / name
    if not f.exists():
        pytest.skip(f"{name} not present")
    src = f.read_text(encoding="utf-8", errors="replace").lower()
    hits = [pat for pat in ACCESS if pat in src]
    assert not hits, (
        f"{name} accesses calf_sales via {hits}. That table holds 400-900 lb "
        f"cattle; the index is 700-899 lb only, and recompute_fci_daily() "
        f"applies no weight filter of its own.")


def test_the_access_check_can_actually_fail():
    """Guard the guard: prose must pass, a real query must not."""
    prose = "# the cash series in calf_sales cannot reach the index".lower()
    assert not [p for p in ACCESS if p in prose]
    for real in ('select * from calf_sales where state = ?',
                 'db.merge_replace(conn, "calf_sales", cols, vals)',
                 'left join calf_sales c on c.report_date = m.report_date'):
        assert [p for p in ACCESS if p in real.lower()], real


def test_calf_ingest_writes_only_its_own_table():
    """calf_sales.py may write calf_sales and nothing else."""
    src = (REPO / "calf_sales.py").read_text(encoding="utf-8")
    targets = set(re.findall(r'merge_(?:replace|ignore)\(\s*conn,\s*"([a-z_]+)"', src))
    assert targets == {"calf_sales"}, (
        f"calf_sales.py writes to {targets or 'nothing recognisable'}; it must "
        f"write only calf_sales.")
    # And no raw DML that would slip past the helper.
    for verb in ("INSERT INTO mars_sales", "UPDATE mars_sales", "DELETE FROM mars_sales"):
        assert verb.lower() not in src.lower(), f"calf_sales.py contains {verb}"


def test_recompute_reads_mars_sales_only():
    """
    The index is computed from mars_sales. If that ever becomes a UNION with
    the cash table, this fails loudly.
    """
    src = (REPO / "update_index.py").read_text(encoding="utf-8")
    i = src.index("def recompute_fci_daily")
    body = src[i:i + 4000]
    assert "FROM mars_sales" in body
    assert "calf_sales" not in body


def test_stored_index_rows_are_index_brackets_only():
    """
    The data itself, not just the code. mars_sales must contain only the four
    index brackets -- the cash table's 400-650 and 900 must be absent.
    """
    db_path = REPO / "data" / "mars_history.db"
    if not db_path.exists():
        pytest.skip("no local database")
    import sqlite3
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        got = {r[0] for r in conn.execute("SELECT DISTINCT weight_low FROM mars_sales")}
    except sqlite3.OperationalError:
        pytest.skip("mars_sales absent")
    finally:
        conn.close()
    extra = got - INDEX_BRACKETS
    assert not extra, (
        f"mars_sales contains non-index weight brackets {sorted(extra)}. The "
        f"index is 700-899 lb; anything else in this table is published as "
        f"though it qualified.")


WRITE_VERBS = ["INSERT ", "UPDATE ", "DELETE ", "DROP ", "CREATE TABLE",
               "merge_replace", "merge_ignore"]

# The shared lookups on the portal pages. Both read the cash series; barn_basis
# also joins fci_daily to price the basis. Neither may write anything.
READ_ONLY_MODULES = ["cash_calves.py", "barn_basis.py"]


def test_cash_lookup_is_read_only():
    """
    The pages reading the cash series must not write -- cash_calves.py and
    barn_basis.py both. They are lookups, and the table they could damage is the
    one feeding the calf default on the crush page.

    Deliberately a plain loop. The first version of this test had a compound
    assertion tangled enough to be a tautology, which is precisely the failure
    that let the hay ingest collapse 444 rows unnoticed -- a check that cannot
    fail is worse than no check, because it is mistaken for coverage.
    """
    portal = REPO.parent / "livestock-portal" / "apps"
    hits = sorted(f for name in READ_ONLY_MODULES
                  for f in portal.glob("*/" + name)) if portal.is_dir() else []
    if not hits:
        pytest.skip("livestock-portal not checked out beside this repo")
    # Without this the glob above would be green while reading nothing: adding a
    # name whose portal copy does not exist leaves `hits` non-empty because the
    # OTHER name matched, so there is no skip and no failure -- the same shape
    # as the three checks in this project that could not fail.
    for name in READ_ONLY_MODULES:
        assert any(f.name == name for f in hits), (
            f"{name} is listed read-only but no portal copy was found; this "
            f"check would pass without ever opening it.")
    for f in hits:
        src = f.read_text(encoding="utf-8")
        found = [v for v in WRITE_VERBS if v in src]
        assert not found, f"{f} contains {found}; the cash lookup must be read-only."


def test_the_read_only_check_can_actually_fail():
    """Guard the guard: prove the verb list catches a write if one appears."""
    assert [v for v in WRITE_VERBS if v in 'cur.execute("INSERT INTO calf_sales ...")']
    assert [v for v in WRITE_VERBS if v in 'db.merge_replace(conn, "mars_sales", ...)']
    assert not [v for v in WRITE_VERBS if v in 'cur.execute("SELECT * FROM calf_sales")']


# ---------------------------------------------------------------------------
# The AMS census reports; it never repairs.
#
# mars_census.py exists because AMS sometimes withdraws or revises a lot and
# merge_ignore can never remove what it replaced. Three attempts at fixing that
# automatically were built and reverted -- the event rate is about one per two
# and a half years, so no threshold in such a module can be calibrated, and the
# guards that make a delete safe are the same guards that refuse a real
# withdrawal. The module is a DETECTOR, and these are what keep it one.
# ---------------------------------------------------------------------------

# A DML verb followed by the table it acts on. Deliberately SQL-shaped rather
# than a ban on the word: the first version of the calf_sales check in this
# file banned a string and promptly failed on a comment explaining the
# isolation, punishing the documentation while proving nothing about the code.
# mars_census.py's own docstring says "this module deletes nothing from
# mars_sales, ever", and that sentence must pass.
#
# UPDATE requires its SET, because bare "update" is an English word -- "update
# the runs row" would otherwise read as DML against a table called "the".
_DML = [
    re.compile(r"\binsert\s+into\s+([a-z_][a-z0-9_]*)", re.I),
    re.compile(r"\bdelete\s+from\s+([a-z_][a-z0-9_]*)", re.I),
    re.compile(r"\bupdate\s+([a-z_][a-z0-9_]*)\s+set\b", re.I),
    re.compile(r"\bmerge\s+into\s+([a-z_][a-z0-9_]*)", re.I),
    re.compile(r"\btruncate\s+table\s+([a-z_][a-z0-9_]*)", re.I),
    re.compile(r"\bdrop\s+table\s+([a-z_][a-z0-9_]*)", re.I),
    re.compile(r"\bcreate\s+table\s+(?:if\s+not\s+exists\s+)?([a-z_][a-z0-9_]*)", re.I),
    # The helpers, which is how a write really happens in this codebase.
    re.compile(r"merge_(?:ignore|replace)\(\s*conn,\s*[\"']([a-z_]+)[\"']", re.I),
    re.compile(r"\btruncate\(\s*conn,\s*[\"']([a-z_]+)[\"']", re.I),
]

CENSUS_TABLES = {"mars_census", "mars_census_runs"}


def dml_targets(text):
    """Every table name this text writes to, however it writes to it."""
    return {m.lower() for pat in _DML for m in pat.findall(text)}


def test_the_census_writes_only_its_own_two_tables():
    """
    The constraint the census was built under: REPORT ONLY. Not argued from
    the docstring -- read off the source, as the exact set of tables any DML
    verb in the file names.

    The wholesale replace goes through db.truncate(), which is
    recompute_fci_daily()'s own precedent, so the row-removing statement lives
    in snowflake_db.py where it always has and this file can be strict rather
    than fuzzy.
    """
    src = (REPO / "mars_census.py").read_text(encoding="utf-8")
    assert dml_targets(src) == CENSUS_TABLES, (
        f"mars_census.py writes to {sorted(dml_targets(src))}; it may write "
        f"only {sorted(CENSUS_TABLES)}. The index table is repaired by hand, "
        f"deliberately -- see the module docstring.")


def test_the_census_reader_writes_nothing_at_all():
    """mars_census_view.py runs inside Streamlit. It is a reader."""
    src = (REPO / "mars_census_view.py").read_text(encoding="utf-8")
    assert dml_targets(src) == set(), sorted(dml_targets(src))


def test_the_dml_scanner_can_actually_fail():
    """
    Guard the guard, which is the rule in CLAUDE.md and which three checks
    written during this work failed. Prose about not deleting must PASS; a
    real write must FAIL, in every spelling this codebase actually uses.
    """
    for prose in (
            "# this module deletes nothing from mars_sales, ever",
            "    Reports only -- nothing here removes or rewrites a row.",
            "# update the runs row before inserting into the findings table",
            "    a truncated report is indistinguishable from a withdrawal",
            '"SELECT report_date FROM mars_sales WHERE raw_date >= ?"'):
        assert dml_targets(prose) == set(), f"prose was read as DML: {prose!r}"

    for real, want in (
            ("DELETE FROM mars_sales WHERE slug_id = ?", "mars_sales"),
            ('cur.execute("delete from mars_sales")', "mars_sales"),
            ('db.merge_replace(conn, "mars_sales", cols, vals)', "mars_sales"),
            ("UPDATE mars_sales SET head_count = 0", "mars_sales"),
            ('db.truncate(conn, "mars_sales")', "mars_sales"),
            ("MERGE INTO mars_sales t USING (SELECT 1) s ON t.a=s.a", "mars_sales"),
            ("DROP TABLE mars_sales", "mars_sales"),
            ('conn.execute("INSERT INTO mars_sales VALUES (?)")', "mars_sales")):
        assert want in dml_targets(real), f"a real write slipped past: {real!r}"


# ---------------------------------------------------------------------------
# The diagnostic exemption, and what pays for it.
#
# barn_report.py reads calf_sales to tell a barn that FILED a report with no
# 700-899 lb cattle in it from a barn that never reported. Those are opposite
# facts -- one day is complete, the other is not -- and "missing" covered both
# until 2026-10-05, when Belen NM filed 19 lots of Medium & Large #1/#1-2
# steers, every one under 700 lb, and the report called it missing on an index
# that was whole, on a morning the number was about to go to clients.
#
# WHY THIS IS SAFE WHERE IT WOULD NOT BE ELSEWHERE. The ban exists because
# recompute_fci_daily() reads mars_sales with no WHERE clause, so anything that
# can put a row there can publish 400 lb calves as index cattle. barn_report
# cannot put a row anywhere: it returns a list of strings, nothing consumes it
# but a log line and a caption, and it issues no write of any kind. The three
# tests below assert each of those rather than taking them on trust -- an
# exemption nobody can check is just a hole.
# ---------------------------------------------------------------------------

def _code_strings(src):
    """
    String literals a module actually executes -- f-string parts included,
    comments absent by construction, docstrings removed.

    AST rather than a text scan for the reason recorded at the top of this
    file: the first version of the calf_sales ban matched a COMMENT explaining
    the isolation and failed on the documentation. Comments are not in an AST,
    and the docstrings here legitimately discuss both tables at once.
    """
    tree = ast.parse(src)
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docs.add(id(body[0].value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docs]


@pytest.mark.parametrize("name", DIAGNOSTIC_MODULES)
def test_a_diagnostic_module_never_writes(name):
    """
    The whole price of the exemption. A diagnostic that can write is an index
    module again, and belongs back in INDEX_MODULES.
    """
    f = REPO / name
    if not f.exists():
        pytest.skip(f"{name} not present")
    src = f.read_text(encoding="utf-8")
    found = [v for v in WRITE_VERBS if v in src]
    assert not found, (
        f"{name} contains {found}. It is exempt from the calf_sales ban only "
        f"because it cannot write; if it can, the exemption is void.")


@pytest.mark.parametrize("name", DIAGNOSTIC_MODULES)
def test_a_diagnostic_module_never_queries_both_tables_at_once(name):
    """
    The creep this exemption has to survive. Reading calf_sales on its own is
    harmless; UNIONing or JOINing it into a query that also reads mars_sales is
    the exact move the ban was written to stop, and it would be invisible in a
    module nobody thinks of as touching the index.
    """
    f = REPO / name
    if not f.exists():
        pytest.skip(f"{name} not present")
    both = [s for s in _code_strings(f.read_text(encoding="utf-8"))
            if "calf_sales" in s.lower() and "mars_sales" in s.lower()]
    assert not both, (
        f"{name} has SQL naming both calf_sales and mars_sales: {both}. The "
        f"cash series may be read beside the index, never joined to it.")


def test_the_diagnostic_checks_can_actually_fail():
    """
    Guard the guards. Both of the above pass trivially on a module that does
    nothing, which is the shape of check this project keeps having to go back
    and fix -- so feed each one a known violation.
    """
    assert [v for v in WRITE_VERBS
            if v in 'cur.execute("INSERT INTO calf_sales ...")']
    assert [v for v in WRITE_VERBS
            if v in 'db.merge_ignore(conn, "mars_sales", cols, vals)']
    assert not [v for v in WRITE_VERBS
                if v in 'cur.execute("SELECT slug_id FROM calf_sales")']

    joined = ('q = "SELECT * FROM mars_sales m JOIN calf_sales c '
              'ON c.report_date = m.report_date"\n')
    assert [s for s in _code_strings(joined)
            if "calf_sales" in s.lower() and "mars_sales" in s.lower()], \
        "a join of the two tables must be detectable"

    # ...and prose about both must NOT trip it, or the check punishes the
    # documentation the way this file's first version did.
    prose = ('"""calf_sales is wider than mars_sales and never reaches it."""\n'
             '# calf_sales must not join mars_sales\n'
             'q = "SELECT slug_id FROM calf_sales"\n')
    assert not [s for s in _code_strings(prose)
                if "calf_sales" in s.lower() and "mars_sales" in s.lower()], \
        "comments and docstrings must not trip the join check"


def test_the_diagnostic_list_and_the_index_list_do_not_overlap():
    """
    A module cannot be both. If one is added back to INDEX_MODULES without
    being taken out of DIAGNOSTIC_MODULES, the ban silently stops applying to
    it -- the parametrised ban would still pass, because it would be testing
    the exempt copy of the name.
    """
    overlap = sorted(set(INDEX_MODULES) & set(DIAGNOSTIC_MODULES))
    assert not overlap, (
        f"{overlap} is listed as both an index module and a diagnostic. Pick "
        f"one: the exemption is meaningless if the ban also claims to cover it.")
