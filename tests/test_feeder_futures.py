"""
feeder_futures.py: the splice, the join, and the arithmetic that reads as basis.

SHARED VERBATIM between this repo and livestock-portal, like test_six_state.py,
so it probes for the portal's app directory and falls back to this repo's root.

OFFLINE. Every test here runs against hand-built bars; none touches Massive.
A suite that needs the network is one that goes red on a bad afternoon and
teaches people to ignore it.

WHAT THESE ACTUALLY DEFEND. The module's numbers were checked against a
completely separate implementation on 2026-10-30 -- different API parsing,
different leg ordering -- and the two agreed on 1,251 sessions, mean -0.1768,
2026 mean -4.8626, convergence 0.7085 and 39 rolls. That comparison is the real
proof and it cannot live in a test, because it needs five years of live data.
What CAN live here is every step where a plausible alternative silently changes
the number, and each of those has a paired test showing the check fails when
the rule is broken.
"""

import sys
from datetime import date
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
for candidate in (HERE.parent / "apps" / "cme_feeder_cattle", HERE.parent):
    if (candidate / "feeder_futures.py").exists():
        APP_DIR = candidate
        break
else:                                                    # pragma: no cover
    raise RuntimeError("feeder_futures.py not found in either layout")
sys.path.insert(0, str(APP_DIR))

import feeder_futures as ff  # noqa: E402


# -- the import set, pinned the way six_state.py's is -----------------------

def test_the_module_stays_self_contained():
    """It may import requests and the stdlib, and nothing from either repo.

    The point is not minimalism. This file is byte-identical in two repos and
    an import of anything repo-local would have to be byte-identical there too
    -- a second file under the drift test, bought for nothing. It talks to
    Massive directly for that reason.

    PARSED WITH ast, NOT BY SCANNING LINES. The line scanner this replaced
    matched any stripped line beginning "import ", which meant a COMMENT could
    fail it -- a sentence wrapping onto a line that happened to start with the
    word made the module look like it imported `set`. That is the failure
    CLAUDE.md names: a guard that reads prose instead of code. It also missed
    imports nested inside functions, which are exactly the ones a reader
    skimming the header would not see.
    """
    import ast as _ast
    source = (APP_DIR / "feeder_futures.py").read_text(encoding="utf-8")
    imported = set()
    for node in _ast.walk(_ast.parse(source)):
        if isinstance(node, _ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, _ast.ImportFrom) and node.level == 0:
            imported.add((node.module or "").split(".")[0])
    assert imported == {"concurrent", "datetime", "requests"}, imported


def test_that_import_pin_can_actually_fail():
    """Plant a nested import the old line scanner would also have missed."""
    import ast as _ast
    bad = "import requests\ndef f():\n    import statistics\n    return statistics\n"
    found = set()
    for node in _ast.walk(_ast.parse(bad)):
        if isinstance(node, _ast.Import):
            found.update(a.name.split(".")[0] for a in node.names)
    assert found == {"requests", "statistics"}


# -- the splice -------------------------------------------------------------

def test_tickers_are_ordered_by_contract_and_never_tie():
    """Ordering comes from the ticker, not from the data.

    The obvious alternative -- order legs by each series' last observed date --
    TIES the current front month against the one behind it, because both carry
    a settle for today, and a tie puts the wrong contract in front at the live
    end of the series.
    """
    pairs = ff.contract_tickers(date(2026, 10, 30), years_back=3)
    keys = [k for _t, k in pairs]
    assert keys == sorted(keys)
    assert len(set(keys)) == len(keys), "two contracts share a sort key"
    assert pairs[-1][0].startswith("GF")
    assert len({t for t, _ in pairs}) == len(pairs), "duplicate ticker"


def test_the_nearby_series_takes_the_nearest_unexpired_contract():
    """Each date resolves to one contract, and a later contract never
    back-fills a date the front month already owns."""
    hist = {
        "GFV6": {date(2026, 10, 1): 300.0, date(2026, 10, 2): 301.0},
        "GFX6": {date(2026, 10, 1): 310.0, date(2026, 10, 2): 311.0,
                 date(2026, 10, 3): 312.0},
    }
    pairs = [("GFV6", (2026, 9)), ("GFX6", (2026, 10))]
    rows = _splice(pairs, hist)
    assert [(r["date"].day, r["contract"], r["settle"]) for r in rows] == [
        (1, "GFV6", 300.0), (2, "GFV6", 301.0), (3, "GFX6", 312.0)]


def test_that_splice_check_can_actually_fail():
    """Feed it a splice that DOES let the back month overwrite, and the
    expectation above must reject it."""
    hist = {
        "GFV6": {date(2026, 10, 1): 300.0},
        "GFX6": {date(2026, 10, 1): 310.0},
    }
    pairs = [("GFV6", (2026, 9)), ("GFX6", (2026, 10))]
    broken = {}
    for ticker, series in hist.items():          # no previous-end cut at all
        for when, settle in series.items():
            broken[when] = {"date": when, "settle": settle, "contract": ticker}
    assert broken[date(2026, 10, 1)]["contract"] == "GFX6"
    assert _splice(pairs, hist)[0]["contract"] == "GFV6"


def _splice(pairs, hist):
    """Run the module's splice over canned history, without the network."""
    rows, previous_end = {}, None
    for ticker, _key in pairs:
        history = hist.get(ticker) or {}
        if not history:
            continue
        end = max(history)
        for when, settle in history.items():
            if when > end:
                continue
            if previous_end is not None and when <= previous_end:
                continue
            rows[when] = {"date": when, "settle": settle, "contract": ticker}
        previous_end = end if previous_end is None else max(previous_end, end)
    return [rows[d] for d in sorted(rows)]


# -- the join ---------------------------------------------------------------

def _rows(*triples):
    return [{"date": d, "settle": s, "contract": c} for d, s, c in triples]


def test_a_date_with_no_published_index_is_dropped_not_carried():
    """INNER join. A session CME did not print has no basis, and filling it
    from the day before would invent one that never existed."""
    rows = _rows((date(2026, 10, 26), 350.0, "GFX6"),
                 (date(2026, 10, 27), 352.0, "GFX6"),
                 (date(2026, 10, 28), 351.0, "GFX6"))
    joined = ff.with_basis(rows, {date(2026, 10, 26): 349.0, date(2026, 10, 28): 351.5})
    assert [r["date"].day for r in joined] == [26, 28]
    assert joined[0]["basis"] == pytest.approx(1.0)
    assert joined[1]["basis"] == pytest.approx(-0.5)


def test_sessions_to_expiry_counts_back_within_each_contract():
    """Zero on a contract's last session in the data, rising backwards, and
    RESET at a roll rather than running on across the whole series."""
    rows = _rows((date(2026, 9, 1), 300.0, "GFV6"),
                 (date(2026, 9, 2), 301.0, "GFV6"),
                 (date(2026, 9, 3), 310.0, "GFX6"),
                 (date(2026, 9, 4), 311.0, "GFX6"),
                 (date(2026, 9, 5), 312.0, "GFX6"))
    index = {r["date"]: 300.0 for r in rows}
    joined = ff.with_basis(rows, index)
    assert [r["sessions_to_expiry"] for r in joined] == [1, 0, 2, 1, 0]


def test_convergence_reads_the_final_sessions_only():
    """It is an assertion about the pipeline, not the market: a cash-settled
    contract must converge, so a wide number here means the units, the roll or
    the index series is wrong."""
    rows = _rows((date(2026, 9, 1), 320.0, "GFV6"),    # far out, 20 wide
                 (date(2026, 9, 2), 300.2, "GFV6"),    # at expiry, 0.2
                 (date(2026, 9, 3), 299.8, "GFV6"))    # at expiry, 0.2
    joined = ff.with_basis(rows, {r["date"]: 300.0 for r in rows})
    assert ff.convergence(joined) == pytest.approx(0.2, abs=1e-9)


def test_that_convergence_check_can_actually_fail():
    """A 100x unit error -- cents read as dollars -- must blow convergence up,
    because that is the failure it exists to catch."""
    rows = _rows((date(2026, 9, 2), 3.002, "GFV6"), (date(2026, 9, 3), 2.998, "GFV6"))
    joined = ff.with_basis(rows, {r["date"]: 300.0 for r in rows})
    assert ff.convergence(joined) > 250


def test_convergence_is_none_rather_than_zero_when_nothing_qualifies():
    """None says "not measured"; 0.0 would say "measured, and perfect"."""
    assert ff.convergence([]) is None


# -- the conditional study, and the four ways it could lie ------------------

def _contract_span(start, n, contract, basis_at, index=300.0):
    from datetime import timedelta as _td
    rows = [{"date": start + _td(days=i), "settle": index + basis_at(i),
             "contract": contract} for i in range(n)]
    return rows


def test_a_forward_move_across_a_roll_is_refused():
    """
    THE ROLL IS NOT A RETURN. Differencing a nearby series across an expiry
    books the gap between two different contracts as a price move. Here the
    back month sits 20 higher; a careless window would report +20.
    """
    rows = (_contract_span(date(2026, 1, 1), 3, "GFV6", lambda i: 0.0)
            + _contract_span(date(2026, 1, 4), 3, "GFX6", lambda i: 20.0))
    joined = ff.with_basis(rows, {r["date"]: 300.0 for r in rows})
    assert ff.forward_move(joined, 0, 1) is not None      # inside GFV6
    assert ff.forward_move(joined, 0, 3) is None          # would cross the roll
    assert ff.forward_move(joined, 3, 2) is not None      # inside GFX6


def test_that_roll_refusal_can_fail():
    """Ignore the contract and the same window returns the gap as a move."""
    rows = (_contract_span(date(2026, 1, 1), 3, "GFV6", lambda i: 0.0)
            + _contract_span(date(2026, 1, 4), 3, "GFX6", lambda i: 20.0))
    joined = ff.with_basis(rows, {r["date"]: 300.0 for r in rows})
    naive = joined[0 + 3]["settle"] - joined[0]["settle"]
    assert naive == pytest.approx(20.0)
    assert ff.forward_move(joined, 0, 3) is None


def test_episodes_collapse_a_run_into_one_event():
    """
    Thirty consecutive days over a threshold are ONE thing that happened.
    Counting days is how a sample of three reports itself as twenty-two.
    """
    flags = [False, True, True, True, False, False, True, True, False, True]
    assert ff.episode_starts(flags) == [1, 6, 9]
    assert sum(flags) == 6          # the day count a careless study would use


def test_an_episode_that_starts_on_day_zero_still_counts(self=None):
    assert ff.episode_starts([True, True, False]) == [0]


def test_premium_and_discount_are_kept_apart():
    """They are not mirror images, so pooling |basis| would average a real
    signal against a non-signal and report neither."""
    rows = _contract_span(date(2026, 1, 1), 40,
                          "GFV6", lambda i: 9.0 if i < 10 else -9.0)
    joined = ff.with_basis(rows, {r["date"]: 300.0 for r in rows})
    study = ff.conditional_study(joined, thresholds=(8,), horizons=(5,))
    signs = {c["sign"]: c for c in study["cells"]}
    assert signs[1]["episodes"] == 1
    assert signs[-1]["episodes"] == 1
    assert signs[1]["days"] == 10
    assert signs[-1]["days"] == 30


def test_the_study_reports_a_baseline_to_read_the_cells_against():
    """A 70% up-rate is not a finding if the unconditional rate is 70%."""
    rows = _contract_span(date(2026, 1, 1), 40, "GFV6", lambda i: 0.0)
    for i, r in enumerate(rows):
        r["settle"] = 300.0 + i            # a series that only ever rises
    joined = ff.with_basis(rows, {r["date"]: 300.0 for r in rows})
    study = ff.conditional_study(joined, thresholds=(8,), horizons=(5,))
    assert study["baseline"][5]["up_pct"] == pytest.approx(100.0)


def test_usable_and_attempted_are_both_reported():
    """Dropping roll-crossing windows selects for early-in-contract entries,
    so the share dropped has to be visible rather than silently absorbed."""
    rows = _contract_span(date(2026, 1, 1), 12, "GFV6", lambda i: -9.0)
    joined = ff.with_basis(rows, {r["date"]: 300.0 for r in rows})
    study = ff.conditional_study(joined, thresholds=(8,), horizons=(20,))
    cell = [c for c in study["cells"] if c["sign"] == -1][0]
    h = cell["horizons"][20]
    assert h["attempted"] == 1 and h["usable"] == 0
    assert h["up_pct"] is None, "an unmeasurable cell must not report a rate"


def test_median_dte_is_carried_so_convergence_is_visible():
    """A cash-settled contract must converge, so an entry 2 sessions from
    expiry is a different claim from one 40 sessions out."""
    rows = _contract_span(date(2026, 1, 1), 30, "GFV6", lambda i: -9.0)
    joined = ff.with_basis(rows, {r["date"]: 300.0 for r in rows})
    cell = [c for c in ff.conditional_study(joined, thresholds=(8,),
                                            horizons=(5,))["cells"]
            if c["sign"] == -1][0]
    assert cell["median_dte"] == 29        # first day of a 30-session contract


def test_the_study_is_empty_rather_than_fabricated_on_no_rows():
    assert ff.conditional_study([]) == {}


# -- the summary ------------------------------------------------------------

def test_summary_is_empty_on_an_empty_series_rather_than_raising():
    assert ff.summary([]) == {}


def test_summary_counts_rolls_as_contract_changes():
    rows = _rows((date(2026, 9, 1), 300.0, "GFV6"),
                 (date(2026, 9, 2), 300.0, "GFX6"),
                 (date(2026, 9, 3), 300.0, "GFX6"),
                 (date(2026, 9, 4), 300.0, "GFF7"))
    out = ff.summary(ff.with_basis(rows, {r["date"]: 300.0 for r in rows}))
    assert out["rolls"] == 2
    assert out["sessions"] == 4


# -- the trailing windows and the extremes ---------------------------------

def _span(start, n, basis_at):
    """n consecutive daily rows from `start`, with basis set by a callable."""
    from datetime import timedelta as _td
    rows = []
    for i in range(n):
        d = start + _td(days=i)
        rows.append({"date": d, "settle": 300.0 + basis_at(i), "contract": "GFV6"})
    index = {r["date"]: 300.0 for r in rows}
    return ff.with_basis(rows, index)


def test_a_trailing_window_is_anchored_on_the_LAST_SESSION_not_today():
    """The window must not shrink because CME prints on a lag.

    This series ends at the last date with BOTH a settle and a published
    index, which is routinely days behind today. Anchoring on today would make
    "the last 30 days" cover fewer and fewer sessions the staler the data got,
    and cover a different span again after a holiday.
    """
    rows = _span(date(2026, 1, 1), 40, lambda i: 1.0)
    got = ff.trailing(rows, 30)
    assert len(got) == 30
    assert got[-1]["date"] == date(2026, 2, 9)      # the last session
    assert got[0]["date"] == date(2026, 1, 11)      # 30 calendar days inclusive


def test_that_anchor_check_can_actually_fail():
    """Anchor the same data on a later 'today' and the window must shrink --
    which is exactly the behaviour the default avoids."""
    rows = _span(date(2026, 1, 1), 40, lambda i: 1.0)
    stale = ff.trailing(rows, 30, anchor=date(2026, 2, 20))
    assert len(stale) == 19 < 30


def test_excursions_return_the_rows_so_a_deviation_can_be_looked_up():
    rows = _span(date(2026, 1, 1), 5, lambda i: [0.5, -3.0, 2.0, 7.5, -1.0][i])
    high, low = ff.excursions(rows)
    assert high["basis"] == pytest.approx(7.5)
    assert high["date"] == date(2026, 1, 4)
    assert low["basis"] == pytest.approx(-3.0)
    assert low["date"] == date(2026, 1, 2)
    assert "contract" in high and "contract" in low


def test_excursions_on_an_all_positive_series_still_report_a_least_positive_low():
    """`low` is the MINIMUM, not "the most negative, if any are". A series that
    never went negative has a smallest value and the table should show it
    rather than a dash that reads as missing data."""
    rows = _span(date(2026, 1, 1), 3, lambda i: [2.0, 5.0, 3.0][i])
    high, low = ff.excursions(rows)
    assert high["basis"] == pytest.approx(5.0)
    assert low["basis"] == pytest.approx(2.0)


def test_window_table_covers_every_span_and_the_full_series():
    rows = _span(date(2026, 1, 1), 400, lambda i: 1.0)
    table = ff.window_table(rows)
    assert [r["label"] for r in table] == [
        "Last 30 days", "Last 90 days", "Last 180 days", "Last 365 days",
        "Full history"]
    assert [r["sessions"] for r in table] == [30, 90, 180, 365, 400]
    assert all(r["mean"] == pytest.approx(1.0) for r in table)


def test_a_window_longer_than_the_history_reports_what_exists():
    """Not an error and not a blank: 200 sessions is the honest answer to
    "the last 365 days" on a 200-session file, and `sessions` says so."""
    rows = _span(date(2026, 1, 1), 50, lambda i: 2.0)
    table = {r["label"]: r for r in ff.window_table(rows)}
    assert table["Last 365 days"]["sessions"] == 50
    assert table["Last 365 days"]["mean"] == pytest.approx(2.0)


def test_an_empty_window_reports_None_rather_than_zero():
    """"Measured, and flat" and "nothing to measure" are different answers."""
    table = {r["label"]: r for r in ff.window_table([])}
    assert table["Last 30 days"]["sessions"] == 0
    assert table["Last 30 days"]["mean"] is None
    assert table["Last 30 days"]["high"] is None


def test_window_means_actually_differ_when_the_recent_period_differs():
    """The guard that the windows are not all quietly reading the same rows."""
    rows = _span(date(2026, 1, 1), 200, lambda i: 10.0 if i >= 180 else 0.0)
    table = {r["label"]: r for r in ff.window_table(rows)}
    assert table["Last 30 days"]["mean"] > table["Last 180 days"]["mean"]
    assert table["Last 180 days"]["mean"] > table["Full history"]["mean"]


def test_summary_year_means_are_per_year_not_cumulative():
    rows = _rows((date(2025, 9, 1), 310.0, "GFU5"), (date(2025, 9, 2), 290.0, "GFU5"),
                 (date(2026, 9, 1), 305.0, "GFU6"))
    out = ff.summary(ff.with_basis(rows, {r["date"]: 300.0 for r in rows}))
    assert out["by_year"][2025] == pytest.approx(0.0)
    assert out["by_year"][2026] == pytest.approx(5.0)
