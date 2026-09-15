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
PORTAL = HERE.parent / "livestock-portal" / "apps" / "cme_feeder_cattle"

pytestmark = pytest.mark.skipif(
    not PORTAL.is_dir(), reason="livestock-portal not checked out beside this repo")

SHARED = ["index_dates.py", "snowflake_db.py", "bucketing.py",
          "composition.py", "volumes.py", "snapshots.py", "cash_calves.py"]


@pytest.mark.parametrize("name", SHARED)
def test_shared_modules_are_identical(name):
    mine, theirs = HERE / name, PORTAL / name
    if not mine.exists() or not theirs.exists():
        pytest.skip(f"{name} not present in both")
    a = mine.read_text(encoding="utf-8").replace("\r\n", "\n")
    b = theirs.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert a == b, (
        f"{name} has drifted between the two repos. A logic fix applied to one "
        f"copy and not the other is invisible until the live page misbehaves.")


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
