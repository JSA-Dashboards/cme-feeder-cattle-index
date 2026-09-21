"""
The dashboards exist TWICE -- standalone in their own repo, and bundled in
livestock-portal, which is the copy actually deployed. CLAUDE.md warns not to
let them drift, and nothing enforced that until now: the 2026-09-14 headline
fix had to be applied by hand to both, and a fix landing in only one would be
invisible until someone compared the live page against the local one.

Shared modules must stay byte-identical. app.py deliberately does not -- each
standalone calls set_page_config and owns its own chrome -- so those are
checked for the specific shared LOGIC instead of equality.

Skips rather than fails when a repo is absent, so this is not a landmine on a
machine that only has one of them.

Two structural traps this file has fallen into once each:

* A search path that silently EXEMPTS a copy. The first version compared this
  repo against livestock-portal/apps/cme_feeder_cattle only, which skipped the
  copies under every other portal app. `test_no_copy_escapes_the_search_path`
  now derives the copy list a second, independent way and compares them, so a
  module added under a new app cannot slip past the hardcoded directory list.

* Assuming every shared module has a copy in THIS repo. trimmings_qc.py does
  not -- it is shared between livestock-portal and beef-trimmings-dashboard and
  does not appear here at all. Anchoring the comparison on `HERE / name` meant
  it was never compared against anything.
"""
import os
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
REPOS = HERE.parent
PORTAL_ROOT = REPOS / "livestock-portal"
PORTAL_APPS = PORTAL_ROOT / "apps"
PORTAL = PORTAL_APPS / "cme_feeder_cattle"
TRIMMINGS = REPOS / "beef-trimmings-dashboard"

pytestmark = pytest.mark.skipif(
    not PORTAL.is_dir(), reason="livestock-portal not checked out beside this repo")

SHARED = ["index_dates.py", "snowflake_db.py", "bucketing.py",
          "composition.py", "volumes.py", "snapshots.py", "cash_calves.py",
          "barn_basis.py", "trimmings_qc.py", "test_trimmings_qc.py"]

# Every directory a shared module is allowed to live in. Explicit rather than a
# recursive glob, because .venv/Lib/site-packages holds files with some of these
# names and would drown the comparison in unrelated matches.
CODE_DIRS = [
    HERE,
    HERE / "tests",
    PORTAL_ROOT / "tests",
    TRIMMINGS,
    TRIMMINGS / "tests",
] + sorted(d for d in PORTAL_APPS.glob("*") if d.is_dir())

# Directories that never hold a first-party copy.
PRUNE = {".venv", "site-packages", "__pycache__", ".git", ".pytest_cache",
         "output", "node_modules", "_archive", "_backups"}


def _norm(f):
    """File text with line endings normalised -- git rewrites them on checkout."""
    return f.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")


def _copies(name):
    """Every copy of `name`, in the directories this file knows to look in."""
    return [d / name for d in CODE_DIRS if (d / name).is_file()]


def _walk_for(name, repo):
    """Independently locate every copy of `name` under `repo`."""
    found = set()
    for root, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d not in PRUNE]
        if name in filenames:
            found.add((Path(root) / name).resolve())
    return found


@pytest.mark.parametrize("name", SHARED)
def test_every_copy_is_identical(name):
    """
    EVERY copy, not just this repo against one portal app.

    The exemption this guards against matters more than a normal drift would.
    Python caches modules by NAME in sys.modules, so whichever page loads first
    wins and every other page gets ITS copy. Two versions of snowflake_db.py
    therefore means a page can run against another page's connection logic, with
    no error raised and no way to tell from the page which copy it got.
    """
    copies = _copies(name)
    if len(copies) < 2:
        absent = [r.name for r in (PORTAL_ROOT, TRIMMINGS) if not r.is_dir()]
        pytest.skip("{}: only {} copy on disk{}".format(
            name, len(copies),
            " (not checked out: " + ", ".join(absent) + ")" if absent else ""))
    first = _norm(copies[0])
    drifted = [str(f) for f in copies[1:] if _norm(f) != first]
    assert not drifted, (
        "{} has {} copies and these differ from {}: {}. Python caches modules "
        "by name, so the page that loads first decides which copy every other "
        "page gets.".format(name, len(copies), copies[0], ", ".join(drifted)))


def test_no_copy_escapes_the_search_path():
    """
    Derive the copy list a SECOND way and assert it matches CODE_DIRS.

    This is the check that would have caught the original bug in this file. A
    hardcoded directory list stops covering a module the moment someone adds it
    under a new app or a new repo, and the symptom of that is a PASSING test,
    not a failing one -- which is why the list cannot be its own authority.
    """
    for name in SHARED:
        found = set()
        for repo in (HERE, PORTAL_ROOT, TRIMMINGS):
            if repo.is_dir():
                found |= _walk_for(name, repo)
        declared = {f.resolve() for f in _copies(name)}
        assert found == declared, (
            "{}: copies on disk that this file never compares: {}. Add their "
            "directory to CODE_DIRS -- an uncompared copy is free to drift."
            .format(name, sorted(str(p) for p in found - declared)))


def test_the_drift_check_can_actually_fail():
    """
    Guard the guard: prove _norm distinguishes real differences and ignores
    line-ending noise. Three checks written during this work could not fail --
    that is the failure this project keeps repeating.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        a, b, c = (Path(t) / n for n in ("a.py", "b.py", "c.py"))
        # write_bytes, not write_text: on Windows the text writer translates
        # "\n" into "\r\n", which would make the CRLF fixture "\r\r\n" and fail
        # this test for a reason that has nothing to do with drift.
        a.write_bytes(b"x = 1\ny = 2\n")
        b.write_bytes(b"x = 1\r\ny = 2\r\n")   # only the line endings differ
        c.write_bytes(b"x = 1\ny = 99\n")      # genuinely different
        assert _norm(a) == _norm(b), "line endings must not count as drift"
        assert _norm(a) != _norm(c), "a real change must count as drift"


def test_the_search_path_check_can_actually_fail():
    """
    Guard that guard too, by feeding it a copy sitting outside the watched set.

    _walk_for must surface the stray, and the comparison must report it. If this
    ever passes trivially, test_no_copy_escapes_the_search_path is decorative.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        watched, stray = Path(t) / "watched", Path(t) / "unwatched_app"
        watched.mkdir()
        stray.mkdir()
        watched.joinpath("shared.py").write_bytes(b"x = 1\n")
        stray.joinpath("shared.py").write_bytes(b"x = 1\n")

        found = _walk_for("shared.py", Path(t))
        declared = {watched.joinpath("shared.py").resolve()}   # misses the stray
        assert found != declared, "an uncompared copy must be detectable"
        assert found - declared == {stray.joinpath("shared.py").resolve()}

        # and a pruned directory must NOT be reported as a stray
        pruned = Path(t) / "__pycache__"
        pruned.mkdir()
        pruned.joinpath("shared.py").write_bytes(b"x = 1\n")
        assert pruned.joinpath("shared.py").resolve() not in _walk_for("shared.py", Path(t))


def test_both_dashboards_use_the_tested_headline_rule():
    """
    Neither copy may reintroduce MAX(report_date) as the headline. That is the
    2026-09-14 bug, and the forecast scorecard cannot see it -- it compares our
    estimate for a date against CME's print for that date, so every date scores
    the same however the headline is chosen.
    """
    for app in (HERE / "app.py", PORTAL / "app.py"):
        src = app.read_text(encoding="utf-8")
        assert "from index_dates import headline_index_date" in src, \
            f"{app} no longer imports the tested headline rule"
        assert "head_pos" in src and "head_date" in src, \
            f"{app} no longer derives its headline from the rule"


def test_both_trimmings_dashboards_use_the_tested_checks():
    """
    The two Beef Trimmings app.py files differ by design -- the standalone owns
    its set_page_config and hides Streamlit's header -- so equality cannot be
    asserted here. Check the shared LOGIC instead.

    These are positive assertions on the real call sites rather than a ban on
    "timedelta(days=8)". A banned string is satisfied by a comment mentioning it
    and broken by a comment mentioning it, which is how a check in this project
    once passed for the wrong reason.
    """
    apps = [a for a in (PORTAL_APPS / "beef_trimmings" / "app.py",
                        TRIMMINGS / "app.py") if a.is_file()]
    if len(apps) < 2:
        pytest.skip("beef-trimmings-dashboard not checked out beside this repo")

    # A copy that has no trimmings_qc.py beside it has not RECEIVED this work
    # yet; it has not broken it. livestock-portal carries the QC module only on
    # feat/trimmings-weekly-overlay, so on master this check would fail every
    # run for a reason nobody can act on -- and a suite that is permanently red
    # teaches people to stop reading it, which costs more than the check earns.
    #
    # Gated on the MODULE being present, not on the import, so this stays a real
    # check: once a copy has trimmings_qc.py, dropping the import fails here as
    # loudly as before. The skip says which copy and why, so "not yet" can never
    # be mistaken for "verified".
    missing = [a for a in apps if not (a.parent / "trimmings_qc.py").is_file()]
    if missing:
        pytest.skip(
            "trimmings_qc.py absent beside "
            + ", ".join(str(a) for a in missing)
            + " -- that copy predates the trimmings QC work rather than having "
              "broken it. Merge the work there and this check runs again.")

    for app in apps:
        src = app.read_text(encoding="utf-8")
        assert "import trimmings_qc as qc" in src, \
            f"{app} no longer imports the tested checks"
        assert "qc.assess_print(" in src, \
            f"{app} no longer flags unrepresentative prints"
        assert "changes = qc.changes" in src, \
            f"{app} defines its own changes() again instead of the tested one"
        # Day and week tiles must compare against the previous OBSERVATION: a
        # date offset steps over the very period it names on an evenly spaced
        # series, which is what made the Australia/NZ tile print the wrong sign.
        assert '"national", [PREV, MONTH, YEAR]' in src, \
            f"{app}: the daily tile stopped using PREV"
        assert '"weekly", [PREV]' in src, \
            f"{app}: the weekly tile stopped using PREV"
        assert src.count('"avg_price", [PREV, MONTH, YEAR]') == 2, \
            f"{app}: an import tile stopped using PREV"
