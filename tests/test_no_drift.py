"""
The dashboard exists TWICE -- standalone here, and bundled in livestock-portal,
which is the copy actually deployed. CLAUDE.md warns not to let them drift, and
nothing enforced that until now: the 2026-09-14 headline fix had to be applied
by hand to both, and a fix landing in only one would be invisible until someone
compared the live page against the local one.

Shared modules must stay byte-identical. app.py deliberately does not -- the
standalone calls set_page_config, loads .env and uses its own palette -- so it
is checked for the specific shared LOGIC instead of equality.

Skips rather than fails when the portal is absent, so this is not a landmine on
a machine that only has one repo.
"""
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
PORTAL_APPS = HERE.parent / "livestock-portal" / "apps"
PORTAL = PORTAL_APPS / "cme_feeder_cattle"

pytestmark = pytest.mark.skipif(
    not PORTAL.is_dir(), reason="livestock-portal not checked out beside this repo")

SHARED = ["index_dates.py", "snowflake_db.py", "bucketing.py",
          "composition.py", "volumes.py", "snapshots.py", "cash_calves.py"]


def _norm(f):
    """File text with line endings normalised -- git rewrites them on checkout."""
    return f.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")


@pytest.mark.parametrize("name", SHARED)
def test_every_copy_is_identical(name):
    """
    EVERY copy, not just this repo against one portal app.

    The first version of this test compared cme-feeder-cattle-index against
    livestock-portal/apps/cme_feeder_cattle only, which silently exempted the
    copies under the OTHER portal apps -- snowflake_db.py lives in five of them
    and cash_calves.py in two.

    That exemption mattered more than a normal drift would. Python caches
    modules by NAME in sys.modules, so whichever page loads first wins and every
    other page gets ITS copy. Two versions of snowflake_db.py therefore means a
    page can run against another page's connection logic, with no error raised
    and no way to tell from the page which copy it got.
    """
    copies = [HERE / name] + sorted(PORTAL_APPS.glob("*/" + name))
    copies = [f for f in copies if f.exists()]
    if len(copies) < 2:
        pytest.skip(name + ": fewer than two copies to compare")
    first = _norm(copies[0])
    drifted = [str(f) for f in copies[1:] if _norm(f) != first]
    assert not drifted, (
        "{} has {} copies and these differ from {}: {}. Python caches modules "
        "by name, so the page that loads first decides which copy every other "
        "page gets.".format(name, len(copies), copies[0], ", ".join(drifted)))


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
