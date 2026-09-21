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
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# Everything that computes, stores or displays the index.
INDEX_MODULES = ["update_index.py", "app.py", "bucketing.py", "snapshots.py",
                 "composition.py", "volumes.py", "reporting.py", "notify_email.py"]

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
    conn = sqlite3.connect(db_path)
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
