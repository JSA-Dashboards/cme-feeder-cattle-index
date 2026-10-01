"""
The AMS census: does it name the two real phantoms, and does it stay silent
about everything else?

BOTH QUESTIONS MATTER EQUALLY. A detector that fires on the two known cases is
worthless if it also fires on an ordinary Tuesday, because the entire product
is that an empty result is the normal one and a non-empty result is worth
interrupting someone for. So the acceptance tests here are paired with
test_the_clean_window_is_silent(), and if that one ever goes noisy the module
is worthless whatever else passes.

FROZEN FIXTURES, NOT LIVE AMS. tests/fixtures/ holds real payloads captured
2026-09-28, trimmed to the fields the census reads. The suite therefore runs
offline and deterministically. The cost, named: if AMS revises slug 1827 or
2022 again, the fixture and live AMS diverge -- which is what a fixture is
for, and the divergence should be read as news about AMS rather than as a
regression here.

Every mutant named in a docstring below is one the scratchpad harness kills.
"""
import ast
import gzip
import json
import math
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import date, timedelta
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(REPO))

import mars_census as mc                                        # noqa: E402
import snowflake_db as db                                       # noqa: E402
import update_index as ui                                       # noqa: E402


# ---------------------------------------------------------------------------
# Fixture plumbing.
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _load_payloads():
    with gzip.open(FIXTURES / "ams_window_2026-09-28.json.gz", "rt",
                   encoding="utf-8") as fh:
        blob = json.load(fh)
    # Narratives are stored once per (slug, report_date) and re-attached here:
    # detect_final_sale_day() reads them, they are identical across every row
    # of one report, and inlining them made the fixture several times larger
    # for no extra coverage.
    narratives = blob["narratives"]
    payloads = {}
    for slug, p in blob["payloads"].items():
        rows = []
        for r in p["results"]:
            r = dict(r)
            nid = r.pop("narrative_id", None)
            r["report_narrative"] = narratives.get(str(nid)) if nid is not None else None
            rows.append(r)
        payloads[int(slug)] = {"stats": p["stats"], "results": rows}
    return payloads, {int(k): v for k, v in blob["locations"].items()}, blob


@lru_cache(maxsize=1)
def _stored_rows():
    """Every mars_sales row in the captured window, as StoredRow objects."""
    _, _, blob = _load_payloads()
    groups = {}
    for r in blob["stored"]:
        row = _row(**r)
        groups.setdefault((row.slug_id, row.raw_date), []).append(row)
    return {g: tuple(rs) for g, rs in groups.items()}


def _row(report_date, raw_date, slug_id, location, weight_low, muscle_grade,
         head_count, avg_weight, avg_price):
    """
    One StoredRow, with the key built HERE.

    WHICH MEANS IT CANNOT PROVE ANYTHING ABOUT load_stored_groups(). This
    helper calls key_of() itself, so the one production line that chooses
    report_date over raw_date is never executed by a test that uses it -- and
    for a while the test named "MUTANT THAT MUST DIE: key on raw_date" was
    built out of these rows and the mutant survived a full mutation run. The
    Ericson test below goes through a real mars_sales and the real loader
    instead; everything else here is about compare(), which takes StoredRows
    as its input and does not care where they came from.
    """
    return mc.StoredRow(
        key=mc.key_of(report_date, slug_id, weight_low, muscle_grade,
                      avg_price, head_count),
        report_date=report_date, raw_date=raw_date, slug_id=slug_id,
        location=location, weight_low=weight_low, muscle_grade=muscle_grade,
        head_count=head_count, avg_weight=avg_weight, avg_price=avg_price,
        index_date=report_date)


# The fixture's own window, read from it rather than restated here -- a second
# copy of these two dates would silently stop describing the payloads the day
# the fixture is recaptured, and the first symptom would be a test that no
# longer judges the groups it names.
#
# 2026-09-08..2026-09-28 is wider than the pipeline's own 14-day refetch, on
# purpose: the Mitchell sale (09-17) falls BELOW the floor of a window opened
# on 09-14, which is exactly the coverage limit judged_window() enforces. The
# real pipeline run on 2026-09-18 had since = 2026-09-03 and would have seen
# it; a run started today never will. That is the honest statement, and this
# fixture is the wider sweep a human would reach for.
WINDOW = (date.fromisoformat(_load_payloads()[2]["since"]),
          date.fromisoformat(_load_payloads()[2]["until"]))


# The two real cases, verbatim from the reports AMS served and from the
# commits that removed them by hand (5083484 and 43a080c).
McALESTER = [
    _row("2026-09-22", "2026-09-22", 1827, "McAlester", 700, "1", 13, 716.0, 368.03),
    _row("2026-09-22", "2026-09-22", 1827, "McAlester", 750, "1", 14, 772.0, 330.29),
    _row("2026-09-22", "2026-09-22", 1827, "McAlester", 800, "1", 4, 833.0, 318.93),
    _row("2026-09-22", "2026-09-22", 1827, "McAlester", 850, "1", 1, 865.0, 318.00),
]
McALESTER_PHANTOM = _row("2026-09-22", "2026-09-22", 1827, "McAlester",
                         750, "1", 15, 771.0, 328.32)

MITCHELL = [
    _row("2026-09-17", "2026-09-17", 2022, "Mitchell", 750, "1", 45, 778.0, 353.72),
    _row("2026-09-17", "2026-09-17", 2022, "Mitchell", 800, "1", 65, 840.0, 336.21),
    _row("2026-09-17", "2026-09-17", 2022, "Mitchell", 850, "1", 117, 882.0, 349.01),
]
MITCHELL_PHANTOM = _row("2026-09-17", "2026-09-17", 2022, "Mitchell",
                        850, "1-2", 6, 853.0, 275.00)


def _slug_fixture(slug_id, extra=()):
    """
    (stored_groups, payloads, locations) for ONE slug, seeded with `extra`.

    The stored side is the fixture's OWN rows for that slug, every sale day of
    them, not just the one the case is about. A slug's other sale days inside
    the window are served too, and leaving them out of the stored side would
    make every lot on them read as missing -- an artefact of the fixture rather
    than anything about the slug, and exactly the kind of noise this module
    exists to avoid producing.
    """
    payloads, locations, _ = _load_payloads()
    stored = {g: rs for g, rs in _stored_rows().items() if g[0] == slug_id}
    for row in extra:
        g = (row.slug_id, row.raw_date)
        stored[g] = stored.get(g, ()) + (row,)
    return stored, {slug_id: payloads[slug_id]}, {slug_id: locations[slug_id]}


# ---------------------------------------------------------------------------
# Which module is actually under test.
# ---------------------------------------------------------------------------

def test_the_census_module_under_test_is_the_one_beside_it():
    """
    NOT A CLAIM ABOUT mars_census. A claim about which mars_census got
    imported, and it is here because the alternative has already cost this
    project a mutation run.

    pytest inserts a test file's own root at the FRONT of sys.path, ahead of
    PYTHONPATH, and the preamble above inserts REPO ahead of that. So a
    mutation harness that copies a mutated module into a sandbox and then runs
    the repository's TRACKED test file loads the ORIGINAL module and reports
    every mutant as surviving -- an all-clear over code nobody executed, which
    is the exact failure shape mars_census.py itself was built to make
    visible. Two of the five gaps this suite was extended to close were
    generated that way and were not gaps at all.

    One line, and it turns that silent wrong answer into a failure. It is not
    a tautology: run from a root with no mars_census.py beside it and the real
    repository on PYTHONPATH, this is the one test that fails, and it names
    both paths.
    """
    assert Path(mc.__file__).resolve() == (REPO / "mars_census.py").resolve(), (
        f"the tests are running against {mc.__file__}, which is not the "
        f"mars_census.py beside this suite at {REPO}")


# ---------------------------------------------------------------------------
# ACCEPTANCE. The two events this module exists for.
# ---------------------------------------------------------------------------

def test_the_mcalester_revision_is_named():
    """
    AMS published slug 1827's 09/22 report with a 750-bracket grade-1 lot of
    15 head at $328.32 and then corrected it to 14 head at $330.29.
    merge_ignore inserted the correction and could not remove what it
    replaced, so we held five lots where AMS serves four.

    Exactly one finding, and it is the superseded row -- not the correction
    that replaced it, and not the three siblings that never moved.
    """
    stored, payloads, locs = _slug_fixture(1827, extra=[McALESTER_PHANTOM])
    assert set(stored[(1827, "2026-09-22")]) == set(McALESTER) | {McALESTER_PHANTOM}, \
        "the fixture's own 09-22 rows are no longer the four this case describes"
    f = mc.compare(stored, payloads, *WINDOW, locations=locs)
    assert [(p.slug_id, p.raw_date, p.weight_low, p.muscle_grade,
             p.head_count, p.avg_price) for p in f.phantom] == [
        (1827, "2026-09-22", 750, "1", 15, 328.32)]
    assert f.missing == ()
    assert f.withheld == ()


def test_the_mitchell_withdrawal_is_named():
    """
    AMS served four qualifying steer lots on slug 2022's 09/17 report at 07:43
    and three at 13:00 the same day. The phantom fourth -- 6 head at $275.00
    against that barn's own $336-353 that morning -- cost 2 cents on two index
    dates and was removed by hand five days later.

    The previous build's correction-flag gate WITHHELD this one. There is no
    such gate here, which is why it is named.
    """
    stored, payloads, locs = _slug_fixture(2022, extra=[MITCHELL_PHANTOM])
    assert set(stored[(2022, "2026-09-17")]) == set(MITCHELL) | {MITCHELL_PHANTOM}, \
        "the fixture's own 09-17 rows are no longer the three this case describes"
    f = mc.compare(stored, payloads, *WINDOW, locations=locs)
    assert [(p.slug_id, p.raw_date, p.weight_low, p.muscle_grade,
             p.head_count, p.avg_price) for p in f.phantom] == [
        (2022, "2026-09-17", 850, "1-2", 6, 275.0)]
    assert f.missing == ()
    assert f.withheld == ()


def test_the_real_rows_beside_each_phantom_are_left_alone():
    """
    The collateral question, asked separately because "it found the phantom"
    and "it found ONLY the phantom" are different claims and the second is the
    one that keeps the page readable.
    """
    for slug in (1827, 2022):
        stored, payloads, locs = _slug_fixture(slug)
        f = mc.compare(stored, payloads, *WINDOW, locations=locs)
        assert f.phantom == (), (
            f"the repaired slug {slug} produced "
            f"{[p.describe() for p in f.phantom]}")


def test_the_clean_window_is_silent():
    """
    THE TEST THAT PROTECTS "EMPTY IS THE SIGNAL". All 89 roster payloads
    captured 2026-09-28 against the real stored rows for the same window:
    nothing held that AMS does not serve, nothing served that we do not hold,
    no slug withheld.

    If this ever goes noisy the right response is to find the cause, not to
    add a suppression threshold -- a census that prints two lines every
    morning gets ignored inside a week, and then the real one is invisible.
    """
    payloads, locations, _ = _load_payloads()
    f = mc.compare(_stored_rows(), payloads, *WINDOW, locations=locations)
    assert f.phantom == (), [p.describe() for p in f.phantom]
    assert f.missing == (), [m.describe() for m in f.missing]
    assert f.withheld == (), [w.describe() for w in f.withheld]
    assert f.n_compared > 0, "a silent census that compared nothing is not silent"


# ---------------------------------------------------------------------------
# The distinction that broke the last build.
# ---------------------------------------------------------------------------

# A sentinel, not None: a test of an ABSENT stats block passes {}, and `or` on
# a default would quietly hand it the complete one instead -- a check that
# cannot fail, which is the failure this project keeps repeating.
_DEFAULT_STATS = object()


def _payload(rows, stats=_DEFAULT_STATS):
    if stats is _DEFAULT_STATS:
        stats = {"returnedRows": len(rows), "totalRows": len(rows)}
    return {"stats": stats, "results": rows}


def _served(report_date="09/22/2026", weight=750, grade="1", head=14,
            avg_weight=772.0, price=330.29, cls="Steers",
            frame="Medium and Large", final="Final"):
    return {"report_date": report_date, "report_narrative": None,
            "class": cls, "frame": frame, "muscle_grade": grade,
            "weight_break_low": weight, "final_ind": final,
            "head_count": head, "avg_weight": avg_weight, "avg_price": price,
            "market_location_city": "Testville"}


def test_a_served_row_that_stopped_qualifying_is_not_a_phantom():
    """
    THE FAILURE THAT BROKE THE LAST BUILD. It derived "what AMS serves" from
    the QUALIFYING rows, so a revision that nulled avg_weight on some lots
    while a sibling still qualified kept the group live and then destroyed the
    affected rows as withdrawn -- 182 head gone while AMS was serving every
    one, under a log line reading "AMS no longer serves this lot".

    MUTANT THAT MUST DIE: build served_keys from qualifying_rows(results)
    instead of from the raw results.
    """
    stored = {(9999, "2026-09-22"): (
        _row("2026-09-22", "2026-09-22", 9999, "Testville", 750, "1", 14, 772.0, 330.29),)}
    payloads = {9999: _payload([_served(avg_weight=None)])}
    f = mc.compare(stored, payloads, *WINDOW)
    assert f.phantom == (), "AMS still serves this row; it is not a phantom"
    assert len(f.notes) == 1
    assert "no longer qualifies" in f.notes[0].detail
    assert f.notes[0].kind == mc.NOTE


def test_missing_is_built_from_the_qualifying_rows_only():
    """
    Heifers and 1100 lb Large-frame lots are served and correctly not stored.
    Subtracting stored from the RAW served set would report every one of them
    as "we do not hold this", which is true and useless.

    MUTANT THAT MUST DIE: subtract stored keys from the raw served set.
    """
    payloads = {9999: _payload([
        _served(cls="Heifers"),
        _served(weight=1100, frame="Large"),
        _served(cls="Bulls", weight=450),
    ])}
    f = mc.compare({}, payloads, *WINDOW)
    assert f.missing == (), [m.describe() for m in f.missing]


def test_a_qualifying_row_we_do_not_hold_is_reported_as_missing():
    """The other half of the same check -- the direction must still work."""
    f = mc.compare({}, {9999: _payload([_served()])}, *WINDOW)
    assert len(f.missing) == 1
    assert f.missing[0].kind == mc.MISSING
    assert (f.missing[0].weight_low, f.missing[0].head_count) == (750, 14)


# ---------------------------------------------------------------------------
# The key.
# ---------------------------------------------------------------------------

def test_ericson_saturdays_are_keyed_on_the_monday_they_are_stored_under():
    """
    Slug 1853 sells on SATURDAY and is stored under the following Monday --
    report_date 2026-09-14 over raw_date 2026-09-12 -- because
    shift_weekend_to_monday() is part of the stored primary key.

    MUTANT THAT MUST DIE: key on raw_date. All five stored rows then match
    nothing and the whole sale day reads as withdrawn, which is the single
    worst false positive this module could produce.

    DRIVEN THROUGH load_stored_groups(), AGAINST A REAL mars_sales. That is
    the entire point of this test and it was missing: the rows used to be
    built by _row(), which composes the key itself, so load_stored_groups() --
    the ONLY production code that chooses report_date over raw_date -- was
    never executed and the mutant this docstring names survived the whole
    suite. The Ericson rows go into a table here and come back out through
    the loader, so the production choice is what is under test.
    """
    payloads, locations, blob = _load_payloads()
    rows = [r for r in blob["stored"] if r["slug_id"] == 1853]
    assert rows, "the fixture no longer holds any Ericson rows"
    shifted = [r for r in rows if r["report_date"] != r["raw_date"]]
    assert shifted, "the fixture no longer holds a weekend-shifted Ericson row"

    # WITHOUT THIS THE TEST PASSES FOR THE WRONG REASON, and it did: a shifted
    # group below the window floor is never compared, so keying on raw_date
    # broke nothing here and the mutant survived a full mutation run. The
    # premise has to be asserted, not assumed.
    lo, hi = mc.judged_window(*WINDOW)
    assert any(lo <= date.fromisoformat(r["raw_date"]) <= hi for r in shifted), (
        f"the weekend-shifted Ericson row(s) sit outside the judged window "
        f"{lo}..{hi}, so this test cannot see the key revert to raw_date. "
        f"Recapture the fixture from an earlier `since`.")

    conn = _memory_db()
    for r in rows:
        conn.execute("INSERT INTO mars_sales (report_date, raw_date, slug_id, "
                     "location, state, weight_low, muscle_grade, head_count, "
                     "avg_weight, avg_price) VALUES (?,?,?,?,'NE',?,?,?,?,?)",
                     (r["report_date"], r["raw_date"], r["slug_id"],
                      r["location"], r["weight_low"], r["muscle_grade"],
                      r["head_count"], r["avg_weight"], r["avg_price"]))
    conn.commit()
    stored = mc.load_stored_groups(conn, [1853], *WINDOW)
    conn.close()

    assert stored, "load_stored_groups() returned nothing for slug 1853"
    assert any(r.report_date != r.raw_date
               for rs in stored.values() for r in rs), (
        "the loader no longer carries the weekend shift through")

    f = mc.compare(stored, {1853: payloads[1853]}, *WINDOW,
                   locations={1853: locations[1853]})
    assert f.phantom == (), [p.describe() for p in f.phantom]
    assert f.missing == (), [m.describe() for m in f.missing]


def _ericson_rows():
    """
    The fixture's own weekend-shifted Ericson rows, and the two dates that make
    them useful: the Saturday they were SOLD on and the Monday they are FILED
    under. Read from the fixture rather than restated, so a recapture cannot
    leave the tests below quietly describing rows that are no longer there.
    """
    _, _, blob = _load_payloads()
    rows = [r for r in blob["stored"]
            if r["slug_id"] == 1853 and r["report_date"] != r["raw_date"]]
    assert rows, "the fixture no longer holds a weekend-shifted Ericson row"
    raw = {r["raw_date"] for r in rows}
    rep = {r["report_date"] for r in rows}
    assert len(raw) == len(rep) == 1, (raw, rep)
    return rows, date.fromisoformat(raw.pop()), date.fromisoformat(rep.pop())


def _insert(conn, rows, state="NE"):
    for r in rows:
        conn.execute("INSERT INTO mars_sales (report_date, raw_date, slug_id, "
                     "location, state, weight_low, muscle_grade, head_count, "
                     "avg_weight, avg_price) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (r["report_date"], r["raw_date"], r["slug_id"],
                      r["location"], state, r["weight_low"], r["muscle_grade"],
                      r["head_count"], r["avg_weight"], r["avg_price"]))
    conn.commit()


def test_stored_rows_are_grouped_on_the_sale_day_not_the_report_day():
    """
    load_stored_groups() groups on raw_date -- the day the cattle actually
    sold -- while KEYING each row on report_date. Two different dates doing
    two different jobs in the same function, which is why swapping one for the
    other is invisible on any barn that never sells at the weekend.

    Ericson NE (slug 1853) is the one that tells them apart: it sells on a
    SATURDAY and CME's shift files the sale under the following MONDAY. So
    every Ericson row carries raw_date Saturday and report_date Monday, and a
    group keyed on the wrong one is off by two days.

    WHY THAT IS NOT COSMETIC. compare() takes the group's date as the sale
    date and measures it against judged_window()'s floor -- the floor that
    exists because a stored group at sale date R was produced by a report
    dated somewhere in [R-6, R], so R is only judgeable when that whole span
    sat inside the query. Group on report_date and the floor is applied to a
    date that has already been shifted FORWARD, so a sale day the run was
    never entitled to an opinion about gets one anyway, and a report nobody
    asked for is indistinguishable from a lot AMS withdrew. A whole sale day
    arriving as phantoms is the one false positive that would make this module
    worth ignoring.

    MUTANT THAT MUST DIE: groups.setdefault((slug_id, report_date), ...).

    DRIVEN THROUGH load_stored_groups() AGAINST A REAL mars_sales, never
    through _row(), which composes its own key and its own grouping and can
    therefore prove nothing about either. That was the flaw in the last test
    of this kind and it let this exact mutant live.
    """
    rows, sale_day, report_day = _ericson_rows()
    assert sale_day < report_day, (sale_day, report_day)

    # A superseded copy of the 84-head lot: Ericson-shaped, and AMS serves no
    # such price. It is the observable consequence -- whether it is reported
    # depends entirely on which of the two dates the floor is applied to.
    stale = dict(next(r for r in rows if r["weight_low"] == 750),
                 avg_price=361.00)

    # A window whose floor lands exactly on the REPORT day, and therefore
    # strictly after the SALE day. Derived from the two dates rather than
    # typed, so it keeps meaning this when the fixture is recaptured.
    until = WINDOW[1]
    narrow = report_day - timedelta(days=mc.MAX_DATE_SHIFT_DAYS)
    lo, hi = mc.judged_window(narrow, until)
    assert lo == report_day and sale_day < lo, (
        f"this window no longer separates the two dates: floor {lo}, "
        f"sale {sale_day}, report {report_day}")

    conn = _memory_db()
    _insert(conn, rows + [stale])
    stored = mc.load_stored_groups(conn, [1853], narrow, until)
    conn.close()

    # THE GROUPING ITSELF, stated directly.
    assert set(stored) == {(1853, sale_day.isoformat())}, (
        f"load_stored_groups() grouped Ericson under {sorted(stored)} rather "
        f"than the sale day {sale_day}")
    for (_slug, day), group in stored.items():
        for row in group:
            assert row.raw_date == day, (row.raw_date, day)
            assert row.report_date == report_day.isoformat(), row.report_date

    # AND THE CONSEQUENCE. The sale day sits below the floor, so this run is
    # not entitled to an opinion about it and must say nothing -- not even
    # about the stale row, which really is stale. Grouping on report_date
    # steals that entitlement and the row is condemned on a day the query
    # never covered.
    payloads, locations, _ = _load_payloads()
    f = mc.compare(stored, {1853: payloads[1853]}, narrow, until,
                   locations={1853: locations[1853]})
    assert f.phantom == (), (
        f"a sale day below the floor {lo} was judged anyway: "
        f"{[p.describe() for p in f.phantom]}")

    # GUARD THE GUARD. The silence above must come from the floor and not from
    # a stale row this comparison cannot see at all, so widen the window until
    # the sale day is inside it and watch the same row be named.
    wide = sale_day - timedelta(days=mc.MAX_DATE_SHIFT_DAYS)
    conn = _memory_db()
    _insert(conn, rows + [stale])
    stored_wide = mc.load_stored_groups(conn, [1853], wide, until)
    conn.close()
    f_wide = mc.compare(stored_wide, {1853: payloads[1853]}, wide, until,
                        locations={1853: locations[1853]})
    assert [(p.slug_id, p.weight_low, p.avg_price) for p in f_wide.phantom] == \
           [(1853, 750, 361.00)], [p.describe() for p in f_wide.phantom]


def _weekday_control():
    """
    A barn from the same capture that sells on a WEEKDAY, as
    (slug_id, rows, day).

    THE CONTROL EXISTS TO STOP THE TEST PASSING FOR THE WRONG REASON. On a
    barn whose report_date equals its raw_date the two grouping keys are the
    same tuple, so this slug's group must NOT move when the mutant is applied
    while Ericson's does. Chosen from the fixture by a deterministic rule
    rather than named, so a recapture re-picks instead of quietly describing a
    slug that is no longer there.
    """
    _, _, blob = _load_payloads()
    days = {}
    for r in blob["stored"]:
        if r["report_date"] == r["raw_date"] and r["slug_id"] != 1853:
            days.setdefault((r["slug_id"], r["raw_date"]), []).append(r)
    assert days, "the capture no longer holds an unshifted stored row"
    (slug_id, day), rows = max(days.items(), key=lambda kv: (len(kv[1]), -kv[0][0]))
    return slug_id, rows, date.fromisoformat(day)


def test_a_weekend_barn_regroups_and_a_weekday_barn_cannot_tell_the_difference():
    """
    THE CONTROL THE TEST ABOVE CANNOT CARRY. That test dies on its FIRST
    assertion -- the dict keys -- so nothing after that line ever runs on the
    mutant, and its fixture is Ericson alone, every row of which is
    weekend-shifted. A grouping key that swapped the two dates for ALL barns
    and one that swapped them only where they differ are the same failure
    there. A weekday barn in the same load says which.

    Ericson NE (1853) sells on a SATURDAY and Rule 10203.A.1's shift files it
    under the following MONDAY, so its rows carry raw_date Saturday and
    report_date Monday. The control barn sells on a weekday, where the two
    dates are the same string and no grouping key can tell them apart.

    MUTANT THAT MUST DIE: groups.setdefault((slug_id, report_date), ...).
    Ericson's group moves two days forward; the control's does not move at
    all, which is the whole reason this swap is invisible in production until
    a weekend sale lands on a window edge.

    No compare(), no payloads, no window arithmetic -- just the loader's own
    output, so a failure here points at one line.
    """
    ericson, sale_day, report_day = _ericson_rows()
    assert sale_day < report_day, (sale_day, report_day)
    ctrl_slug, ctrl_rows, ctrl_day = _weekday_control()
    assert all(r["report_date"] == r["raw_date"] for r in ctrl_rows), (
        "the control barn is weekend-shifted too and controls nothing")

    conn = _memory_db()
    _insert(conn, ericson)
    _insert(conn, ctrl_rows)
    stored = mc.load_stored_groups(conn, [1853, ctrl_slug], *WINDOW)
    conn.close()

    assert set(stored) == {(1853, sale_day.isoformat()),
                           (ctrl_slug, ctrl_day.isoformat())}, (
        f"grouped under {sorted(stored)}; expected Ericson on its sale day "
        f"{sale_day} (NOT its report day {report_day}) and slug {ctrl_slug} "
        f"on {ctrl_day}")

    # The shift is still carried on the rows -- grouping on the sale day must
    # not have cost the report_date the primary key is built from.
    for row in stored[(1853, sale_day.isoformat())]:
        assert row.raw_date == sale_day.isoformat()
        assert row.report_date == report_day.isoformat()
    for row in stored[(ctrl_slug, ctrl_day.isoformat())]:
        assert row.raw_date == row.report_date == ctrl_day.isoformat()


def test_a_real_phantom_on_a_judged_sale_day_is_not_lost_over_the_ceiling():
    """
    THE FALSE NEGATIVE, which is the direction the test above cannot reach.
    That one puts the FLOOR above the sale day and watches the mutant judge a
    day it should not -- a false positive. The mirror image is worse to live
    with.

    judged_window() returns (since + 6, until). Put `until` ON the Saturday
    sale day and the ceiling falls BETWEEN the two dates: the sale day is
    inside the judged window and the Monday report day is outside it. Group on
    raw_date -- correct -- and Ericson is judged and the stale row is named.
    Group on report_date and the group sits above the ceiling, never enters
    `judged`, is never compared, and compare() returns clean.

    That is a phantom sitting in mars_sales, counted by recompute_fci_daily()
    with no WHERE clause, moving every index date whose 7-day window touches
    it -- while the census says nothing is wrong. This module's whole value is
    that an empty result means something.

    MUTANT THAT MUST DIE: groups.setdefault((slug_id, report_date), ...).

    The assertion is on compare()'s OUTPUT, not on the loader's dict, so it
    stands on the consequence rather than on the implementation detail.
    """
    rows, sale_day, report_day = _ericson_rows()
    payloads, locations, _ = _load_payloads()

    # A superseded copy of the 84-head lot at a price AMS serves nowhere on
    # this slug: stored, real-looking, and genuinely stale.
    stale = dict(next(r for r in rows if r["weight_low"] == 750),
                 avg_price=361.00)

    # since + 6 == the sale day, until == the sale day. Derived from the
    # fixture's own dates so a recapture cannot leave this describing a window
    # that no longer separates them.
    since = sale_day - timedelta(days=mc.MAX_DATE_SHIFT_DAYS)
    lo, hi = mc.judged_window(since, sale_day)
    assert lo == sale_day, f"the floor moved off the sale day: {lo}"
    assert hi == sale_day, f"the ceiling moved off the sale day: {hi}"
    assert hi < report_day, (
        f"this window no longer puts the ceiling {hi} between the sale day "
        f"{sale_day} and the report day {report_day}")

    conn = _memory_db()
    _insert(conn, rows + [stale])
    stored = mc.load_stored_groups(conn, [1853], since, sale_day)
    conn.close()
    assert stored, (
        "load_stored_groups() returned nothing -- the rows fell out of the "
        "READ window, so this test would pass on any grouping key at all")

    f = mc.compare(stored, {1853: payloads[1853]}, since, sale_day,
                   locations={1853: locations[1853]})
    assert [(p.slug_id, p.weight_low, p.avg_price) for p in f.phantom] == \
           [(1853, 750, 361.00)], (
        f"the stale 750 lot on sale day {sale_day} was not reported. The "
        f"judged window is {f.window_start}..{f.window_end} and the sale day "
        f"is inside it; a group filed under the report day {report_day} sits "
        f"above the ceiling and is never compared. Findings: "
        f"{[p.describe() for p in f.phantom]}")
    assert f.n_compared, "nothing was compared at all"
    assert f.withheld == (), [w.describe() for w in f.withheld]


def test_the_ceiling_test_is_not_passing_on_a_payload_that_serves_nothing():
    """
    GUARD THE GUARD. The test above reports a phantom; it must be reporting it
    because AMS does not serve that lot, not because AMS served nothing this
    comparison could match. Feed the same window the UNMODIFIED Ericson rows
    and the same payload: every one of them is served, so the run is silent.

    Without this, a payload that had gone empty (or a fixture recapture that
    emptied it) would make the test above pass by condemning all five real
    lots, and the pair would look like a working detector while detecting
    nothing. Falsifiable, not assumed: a probe mutant that builds served_keys
    from an empty iterable kills this test.
    """
    rows, sale_day, _report_day = _ericson_rows()
    payloads, locations, _ = _load_payloads()
    since = sale_day - timedelta(days=mc.MAX_DATE_SHIFT_DAYS)

    conn = _memory_db()
    _insert(conn, rows)
    stored = mc.load_stored_groups(conn, [1853], since, sale_day)
    conn.close()

    f = mc.compare(stored, {1853: payloads[1853]}, since, sale_day,
                   locations={1853: locations[1853]})
    assert f.phantom == (), (
        f"the real Ericson rows were condemned, so the phantom reported by "
        f"the test above says nothing about the stale row: "
        f"{[p.describe() for p in f.phantom]}")
    assert f.n_compared, "the sale day was not compared, so this proves nothing"


def test_a_revision_that_only_moves_the_head_count_is_still_a_phantom():
    """
    head_count is part of mars_sales' primary key --

        (report_date, slug_id, weight_low, muscle_grade, avg_price, head_count)

    -- so a revision that changes ONLY the head count writes a second row
    beside the first, merge_ignore cannot remove the one it replaced, and the
    index counts both.

    THIS IS THE McALESTER SHAPE. AMS published slug 1827's 09/22 report with a
    750-bracket grade-1 lot and then corrected it; both copies sat in
    mars_sales and 15 phantom head moved three index dates. And a head-count-
    ONLY revision is live, not hypothetical: on 2026-09-28 Oklahoma City's
    report carried 1,057 qualifying head as Preliminary and 1,079 as Final.

    So the case here is the real corrected McAlester lot -- same day, same
    bracket, same grade, same price -- with the head count as the ONE field
    that moved, which is the narrowest version of the event this module was
    built to surface.

    MUTANT THAT MUST DIE: drop head_count from key_of(). The stale copy and
    the correction then produce one key, the two rows collapse, and the
    phantom is never reported.
    """
    correction = next(r for r in McALESTER if r.weight_low == 750)
    assert (correction.head_count, correction.avg_price) == (14, 330.29)
    stale = _row(correction.report_date, correction.raw_date, 1827,
                 "McAlester", 750, "1", correction.head_count + 1,
                 correction.avg_weight, correction.avg_price)
    assert stale.key != correction.key, (
        "head_count is not in the key, so a head-count-only revision is "
        "invisible to the census")

    stored, payloads, locs = _slug_fixture(1827, extra=[stale])
    f = mc.compare(stored, payloads, *WINDOW, locations=locs)
    assert [(p.slug_id, p.raw_date, p.weight_low, p.muscle_grade,
             p.head_count, p.avg_price) for p in f.phantom] == [
        (1827, "2026-09-22", 750, "1", 15, 330.29)]
    assert f.missing == () and f.withheld == () and f.notes == ()


def test_a_head_count_only_revision_shows_up_in_both_directions():
    """
    THE SAME EVENT WITH THE CORRECTION NOT YET HELD, which is a strictly
    stronger statement about the key than the test above can make.

    That test holds BOTH copies -- the stale one and the correction -- which
    is the state merge_ignore leaves behind and so is the right acceptance
    case. It therefore asserts f.missing == (): AMS's correction matches the
    stored correction, and only the superseded copy is unaccounted for. Here
    the ingest declined the correction on a primary-key collision, or it
    landed after that day's last merge, so the SAME head-count difference has
    to be reported TWICE OVER and under two different words:

        phantom   the 15-head copy we hold, which AMS serves nowhere
        missing   the 14-head lot AMS serves, which we hold nowhere

    MUTANT THAT MUST DIE: drop head_count from key_of(). The stale copy and
    the served correction then produce ONE key, the comparison finds a
    perfect match on both sides at once, and BOTH lines vanish -- not a
    mis-worded finding but a silent all-clear over a 15-head phantom that
    moved three index dates.

    Synthetic slug and the file's own _served() defaults, whose shape is the
    corrected McAlester lot: 750 bracket, grade 1, 14 head, $330.29.
    """
    stale = _row("2026-09-22", "2026-09-22", 9999, "Testville",
                 750, "1", 15, 772.0, 330.29)
    payloads = {9999: _payload([_served(head=14)])}
    f = mc.compare({(9999, "2026-09-22"): (stale,)}, payloads, *WINDOW)

    assert [(p.kind, p.weight_low, p.muscle_grade, p.head_count, p.avg_price)
            for p in f.phantom] == [(mc.PHANTOM, 750, "1", 15, 330.29)], \
        [p.describe() for p in f.phantom]
    assert [(m.kind, m.weight_low, m.muscle_grade, m.head_count, m.avg_price)
            for m in f.missing] == [(mc.MISSING, 750, "1", 14, 330.29)], \
        [m.describe() for m in f.missing]
    assert f.withheld == () and f.notes == ()


def test_the_real_mcalester_lot_reports_both_directions_too():
    """
    The same proposition against the captured payload rather than a
    hand-built one, so the served side is what AMS really served on
    2026-09-28 and not an assumption about it.

    The fixture's own 09-22 group for slug 1827 is the four corrected lots.
    Swap the 750-bracket one for a copy carrying 15 head -- every other field,
    including avg_weight, left exactly as it is -- and the run must name our
    copy as a phantom and AMS's as missing. The three siblings must stay
    silent: "it found the difference" and "it found ONLY the difference" are
    different claims and the second is the one that keeps the page readable.
    """
    stored, payloads, locs = _slug_fixture(1827)
    day = (1827, "2026-09-22")
    correction = next(r for r in McALESTER if r.weight_low == 750)
    assert (correction.head_count, correction.avg_price) == (14, 330.29)
    assert sorted(r.weight_low for r in stored[day]) == [700, 750, 800, 850], (
        "the fixture's own 09-22 rows for slug 1827 are no longer the four "
        "lots this case describes")

    stale = _row(correction.report_date, correction.raw_date, 1827,
                 "McAlester", 750, correction.muscle_grade,
                 correction.head_count + 1, correction.avg_weight,
                 correction.avg_price)
    stored = dict(stored)
    stored[day] = tuple(r for r in stored[day] if r.weight_low != 750) + (stale,)

    f = mc.compare(stored, payloads, *WINDOW, locations=locs)
    assert [(p.slug_id, p.raw_date, p.weight_low, p.muscle_grade,
             p.head_count, p.avg_price) for p in f.phantom] == [
        (1827, "2026-09-22", 750, "1", 15, 330.29)], \
        [p.describe() for p in f.phantom]
    assert [(m.slug_id, m.raw_date, m.weight_low, m.muscle_grade,
             m.head_count, m.avg_price) for m in f.missing] == [
        (1827, "2026-09-22", 750, "1", 14, 330.29)], \
        [m.describe() for m in f.missing]
    assert f.withheld == () and f.notes == ()


def test_a_revision_that_only_moves_the_muscle_grade_is_still_a_phantom():
    """
    The same argument for the other key component a revision can move on its
    own. muscle_grade is in mars_sales' primary key, AMS regrades lots between
    a Preliminary and a Final report, and Mitchell's withdrawn phantom was a
    grade 1-2 lot sitting beside that barn's grade 1 ones -- so "the same lot,
    regraded" is a shape this data really produces.

    Built from the real corrected McAlester lot again, with the grade as the
    one field that moved: AMS serves 750/grade 1/14 head/$330.29 and we also
    hold a superseded copy of it graded 1-2.

    MUTANT THAT MUST DIE: drop muscle_grade from key_of(). The two rows
    collapse to one key and the stale grade is never reported -- and the same
    mutation would let a dairy or Brahma lot's key equal a steer lot's, which
    is the exclusion CLAUDE.md says is satisfied by construction.
    """
    correction = next(r for r in McALESTER if r.weight_low == 750)
    assert correction.muscle_grade == "1"
    stale = _row(correction.report_date, correction.raw_date, 1827,
                 "McAlester", 750, "1-2", correction.head_count,
                 correction.avg_weight, correction.avg_price)
    assert stale.key != correction.key, (
        "muscle_grade is not in the key, so a regrade is invisible to the "
        "census")

    stored, payloads, locs = _slug_fixture(1827, extra=[stale])
    f = mc.compare(stored, payloads, *WINDOW, locations=locs)
    assert [(p.slug_id, p.raw_date, p.weight_low, p.muscle_grade,
             p.head_count, p.avg_price) for p in f.phantom] == [
        (1827, "2026-09-22", 750, "1-2", 14, 330.29)]
    assert f.missing == () and f.withheld == () and f.notes == ()


# AMS's real spelling, and the whole vocabulary mars_sales holds: 23,081 rows
# graded '1' and 10,205 graded '1-2' on 2026-09-30, and nothing else. CME Rule
# 10203 covers Medium & Large #1 AND #1-2, so both are index-qualifying and
# both routinely appear in one report -- which is exactly why they must not
# share a key.
GRADE_1 = "1"
GRADE_1_2 = "1-2"


def test_a_withdrawn_grade_1_2_lot_is_not_masked_by_its_grade_1_twin():
    """
    THE SAME FIELD, DRIVEN THROUGH THE PRODUCTION KEY COMPOSITION. The test
    above builds both of its rows with _row(), which calls key_of() itself,
    so load_stored_groups() -- the only production code that builds a STORED
    key -- is never executed by it. That is the same hole the Ericson
    docstring already names for report_date, where the mutant survived a full
    run.

    THE EVENT. A barn sells a Medium & Large #1 lot and a #1-2 lot of the same
    weight, the same head count and the same price on the same day -- the pair
    mars_sales really holds, see the live-database test in the last section --
    and AMS then withdraws one of them. We hold both, merge_ignore cannot
    remove what it replaced, and recompute_fci_daily() reads mars_sales with
    no WHERE clause, so the withdrawn lot keeps feeding the published index
    until a human takes it out.

    Without muscle_grade in the key the survivor's key equals the withdrawn
    one's, the withdrawn row matches the served set, and the census reports
    NOTHING -- the exact silent mask this module exists to prevent.

    MUTANT THAT MUST DIE: drop str(muscle_grade).strip() from key_of()'s
    returned tuple.
    """
    conn = _memory_db()
    conn.executemany(
        "INSERT INTO mars_sales (report_date, raw_date, slug_id, location, "
        "state, weight_low, muscle_grade, head_count, avg_weight, avg_price) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        [("2026-09-22", "2026-09-22", 9001, "Testville", "TX", 750, g, 14,
          772.0, 330.29) for g in (GRADE_1, GRADE_1_2)])
    conn.commit()
    window = (date(2026, 9, 8), date(2026, 9, 28))
    stored = mc.load_stored_groups(conn, [9001], *window)
    conn.close()

    held = stored[(9001, "2026-09-22")]
    assert sorted(r.muscle_grade for r in held) == [GRADE_1, GRADE_1_2], (
        "the two rows did not both survive the loader")

    # THE CONTROL, and it is not the discriminator -- say so rather than let a
    # reader count it as evidence. While AMS serves both, neither is a phantom
    # and neither is missing, and that holds with or without the grade in the
    # key. It is here to prove both rows qualify and both are judged, so the
    # silence in the real case below cannot be the fixture failing to arrive.
    both = mc.compare(stored, {9001: _payload(
        [_served(grade=GRADE_1), _served(grade=GRADE_1_2)])}, *window)
    assert both.phantom == (), [p.describe() for p in both.phantom]
    assert both.missing == (), [m.describe() for m in both.missing]
    assert both.notes == () and both.withheld == ()
    assert both.n_compared == 1, "neither row was compared at all"

    # THE EVENT. AMS withdraws the #1-2 lot and still serves the #1 one.
    f = mc.compare(stored, {9001: _payload([_served(grade=GRADE_1)])}, *window)
    assert [(p.slug_id, p.raw_date, p.weight_low, p.muscle_grade,
             p.head_count, p.avg_price) for p in f.phantom] == [
        (9001, "2026-09-22", 750, GRADE_1_2, 14, 330.29)], (
        "the withdrawn #1-2 lot was masked by its #1 twin: "
        f"{[p.describe() for p in f.phantom]}")
    assert f.missing == (), [m.describe() for m in f.missing]
    assert f.withheld == () and f.notes == ()


def test_a_one_cent_price_correction_shows_up_in_both_directions():
    """
    THE SAME EVENT AS THE HEAD-COUNT CASE, on the field AMS revises most
    often. AMS corrects a lot's price by one cent; the ingest either declined
    the correction on a primary-key collision or it landed after that day's
    last merge, so we hold the superseded copy and AMS serves the new one, and
    the census has to report the one difference TWICE OVER under two words:

        phantom   the $256.02 copy we hold, which AMS serves nowhere
        missing   the $256.03 lot AMS serves, which we hold nowhere

    THE PRICE IS DRAWN FROM THE LIVE COLLISION SET, not chosen for the story.
    Both prices are stored in mars_sales and 256.03 * 100 is
    25602.999999999996, so a price_cents() that truncates gives the two lots
    ONE key: the comparison then finds a perfect match on both sides at once
    and BOTH lines vanish -- not a mis-worded finding but a silent all-clear
    over a lot whose published price moved. 275.00/275.01, the pair the key
    test used to rely on, would not have shown this.

    THE STORED SIDE GOES THROUGH load_stored_groups(), the only production
    code that builds a STORED key, for the reason the Ericson and muscle-grade
    docstrings give: a case built out of _row() calls key_of() itself and
    leaves that line unexecuted.

    MUTANT THAT MUST DIE: return int(float(Decimal(str(p))) * 100) from
    price_cents().
    """
    conn = _memory_db()
    conn.execute(
        "INSERT INTO mars_sales (report_date, raw_date, slug_id, location, "
        "state, weight_low, muscle_grade, head_count, avg_weight, avg_price) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("2026-09-22", "2026-09-22", 9001, "Testville", "TX", 750, GRADE_1,
         14, 772.0, 256.02))
    conn.commit()
    window = (date(2026, 9, 8), date(2026, 9, 28))
    stored = mc.load_stored_groups(conn, [9001], *window)
    conn.close()
    assert [r.avg_price for r in stored[(9001, "2026-09-22")]] == [256.02], (
        "the superseded row did not survive the loader")

    # THE CONTROL, and it is not the discriminator -- say so rather than let a
    # reader count it as evidence. While AMS still serves the superseded
    # price, nothing is reported, and that holds whether price_cents() rounds
    # or truncates. It is here to prove the row qualifies and the sale day is
    # judged, so the silence in the real case below cannot be the payload
    # failing to arrive.
    same = mc.compare(stored, {9001: _payload([_served(price=256.02)])}, *window)
    assert same.phantom == () and same.missing == ()
    assert same.notes == () and same.withheld == ()
    assert same.n_compared == 1, "the sale day was not compared at all"

    # THE EVENT. AMS corrects the price by one cent and serves nothing else.
    f = mc.compare(stored, {9001: _payload([_served(price=256.03)])}, *window)
    assert [(p.kind, p.weight_low, p.muscle_grade, p.head_count, p.avg_price)
            for p in f.phantom] == [(mc.PHANTOM, 750, GRADE_1, 14, 256.02)], \
        [p.describe() for p in f.phantom]
    assert [(m.kind, m.weight_low, m.muscle_grade, m.head_count, m.avg_price)
            for m in f.missing] == [(mc.MISSING, 750, GRADE_1, 14, 256.03)], \
        [m.describe() for m in f.missing]
    assert f.withheld == () and f.notes == ()


@pytest.mark.parametrize("p", [275, 275.0, "275.00", Decimal("275"), 275.000])
def test_prices_of_every_type_produce_one_key(p):
    """
    AMS serves 353.72 on one row and a bare int 490 on the next; SQLite hands
    back a REAL and Snowflake's connector something else. Comparing those raw
    is how a key set silently fails to intersect, and an empty intersection
    reads as "AMS withdrew the whole day".

    MUTANT THAT MUST DIE: compare avg_price as a float.
    """
    assert mc.key_of("2026-09-22", 1, 750, "1", p, 6) == \
           mc.key_of("2026-09-22", 1, 750, "1", 275, 6)


def test_a_price_that_drifted_in_its_last_bit_is_still_one_key():
    """
    THE TEST ABOVE IS SATISFIED BY BARE float(), which is exactly why this one
    exists -- it was written after a mutation run showed `return float(p)`
    surviving the type check, because float() collapses 275, "275.00" and
    Decimal("275") just as well.

    What integer cents actually buys is immunity to a value that comes back a
    fraction of a millionth of a cent from the one stored, which is what a
    round trip through a different numeric type can do. No such case has been
    observed in this data -- but the cost of being wrong is not one row, it is
    a whole sale day failing to intersect and reading as withdrawn.

    MUTANT THAT MUST DIE: return float(p) from price_cents().
    """
    drifted = 275.0 + 1e-11
    assert drifted != 275.0, "this fixture no longer drifts; pick a smaller epsilon"
    assert mc.price_cents(drifted) == mc.price_cents(275.0)
    assert mc.key_of("2026-09-22", 1, 750, "1", drifted, 6) == \
           mc.key_of("2026-09-22", 1, 750, "1", 275.0, 6)


def test_a_one_cent_difference_is_a_different_key():
    """
    The other direction: normalisation must not collapse real differences.

    THE ORIGINAL PAIR SAT OFF THE FAILURE. 275.00/275.01 separate whether
    price_cents() rounds or truncates, because 275.01 * 100 lands just ABOVE
    27501 in binary floating point -- so `return int(float(Decimal(str(p))) *
    100)`, truncation, survived this test and a full mutation run with it.

    WHICH adjacent cents survive a float round trip is a property of the
    binary representation and not of the arithmetic, so the pair cannot be
    typed out from taste; it has to be drawn from prices the table really
    holds. 256.02 and 256.03 are such a pair and mars_sales holds both:
    256.03 * 100 is 25602.999999999996, so int() alone files AMS's correction
    under the superseded price's key. 415 of the 12,046 distinct prices in
    mars_sales pair off this way -- re-derived by the two live-database tests
    in the last section, the second of which proves this check can fail.

    A one-cent price correction is the commonest revision AMS makes, and it
    is the McAlester shape one column over.

    MUTANT THAT MUST DIE: return int(float(Decimal(str(p))) * 100) from
    price_cents() -- truncate instead of round.
    """
    assert mc.price_cents(275.00) != mc.price_cents(275.01)
    assert mc.key_of("2026-09-22", 1, 750, "1", 275.00, 6) != \
           mc.key_of("2026-09-22", 1, 750, "1", 275.01, 6)

    # The pair drawn from the live collision set. The cents are asserted as
    # absolute values and not only as a difference: "these two differ" would
    # also pass if the rounding had gone the other way and BOTH prices had
    # landed on 25603, which is the same lot merge under a different name.
    assert (mc.price_cents(256.02), mc.price_cents(256.03)) == (25602, 25603)
    assert mc.key_of("2026-09-22", 1827, 750, "1", 256.02, 14) != \
           mc.key_of("2026-09-22", 1827, 750, "1", 256.03, 14)


# One lot, described once, and the key built from it by keyword. The two
# "differ in exactly one position" tests below each build BOTH of their keys
# from THIS dict with a single field overridden, so "the keys differ" can
# never be true because a second field drifted between two hand-written
# positional calls -- a key that differs for the wrong reason is not evidence
# about the field under test.
ONE_LOT = dict(report_date="2026-09-22", slug_id=1827, weight_low=750,
               muscle_grade=GRADE_1, avg_price=330.29, head_count=14)


def _key_for(**override):
    return mc.key_of(**{**ONE_LOT, **override})


def _one_position_apart(a, b):
    """The indices at which two keys of equal length differ."""
    assert len(a) == len(b), (a, b)
    return [i for i, (x, y) in enumerate(zip(a, b)) if x != y]


def test_two_lots_differing_only_in_head_count_are_two_keys():
    """
    The key-level statement, narrowest form: every component equal except the
    head count, and the keys must still differ.

    MUTANT THAT MUST DIE: drop int(head_count) from key_of()'s returned tuple.

    Stated as "these two differ AND they differ only here", because "they
    differ" alone would also pass if the mutation had broken some other
    component instead.
    """
    assert ONE_LOT["head_count"] == 14
    assert _key_for() == _key_for(head_count=14), (
        "the control failed: this comparison is measuring something other "
        "than the head count")

    fourteen, fifteen = _key_for(head_count=14), _key_for(head_count=15)
    assert fourteen != fifteen, (
        "head_count is not in the key, so a head-count-only revision -- the "
        "McAlester shape -- produces one key for two different lots and the "
        "census can never see it")
    diffs = _one_position_apart(fourteen, fifteen)
    assert len(diffs) == 1, (
        f"these two keys differ in components {diffs}, not in the head count "
        f"alone: {fourteen} vs {fifteen}")
    assert (fourteen[diffs[0]], fifteen[diffs[0]]) == (14, 15), (
        f"the one position that moved holds {fourteen[diffs[0]]!r}/"
        f"{fifteen[diffs[0]]!r}, not the head count")


def test_a_head_count_of_every_type_produces_one_key():
    """
    The other direction, and it is not decoration. AMS serves head_count as a
    bare int, SQLite hands back an INTEGER and a revision can arrive as a
    string; normalising through int() is what stops "14" and 14 being two
    lots -- the failure price_cents() exists to prevent one column over, a
    whole sale day failing to intersect and reading as withdrawn.

    AN EARLIER VERSION OF THIS DOCSTRING SAID str(head_count) "would pass the
    test above". It would not: the sibling fails on that mutant at its final
    assert, where (fourteen[diffs[0]], fifteen[diffs[0]]) is then
    ('14', '15') rather than (14, 15). The sibling is stronger than this
    claimed. Recorded rather than quietly corrected, because the docstrings
    in this file are the record of what each mutant does and a wrong one is
    how a future mutation run talks itself out of a real result.
    """
    assert _key_for(head_count="14") == _key_for(head_count=14)


def test_grade_1_and_grade_1_2_differ_in_exactly_one_key_position():
    """
    The same claim for the other field a revision can move on its own:
    muscle_grade is an element of the tuple key_of() returns.

    MUTANT THAT MUST DIE: drop str(muscle_grade).strip() from key_of()'s
    returned tuple. The two grades then produce one key, a '1-2' lot and a '1'
    lot at the same price on the same report collapse, and the census can
    never see a regrade -- nor a withdrawal of one of the pair.

    THE PREMISES ARE ASSERTED, NOT ASSUMED. key_of() does
    str(muscle_grade).strip(), and a test that passed because of the strip()
    or the str() would prove nothing about the field being IN the tuple. Both
    grades here are already-stripped str, so str().strip() is the identity on
    them and the only thing that can separate the two keys is the field's
    presence.
    """
    for g in (GRADE_1, GRADE_1_2):
        assert isinstance(g, str) and g == g.strip(), (
            f"{g!r} would exercise key_of()'s str()/strip() rather than the "
            f"field's presence in the tuple")
    assert GRADE_1 != GRADE_1_2
    assert _key_for() == _key_for(muscle_grade=GRADE_1), (
        "the control failed: this comparison is measuring something other "
        "than the muscle grade")

    k1, k12 = _key_for(muscle_grade=GRADE_1), _key_for(muscle_grade=GRADE_1_2)
    diffs = _one_position_apart(k1, k12)
    assert len(diffs) == 1, (
        f"grade '1' and grade '1-2' keys differ in {len(diffs)} position(s), "
        f"not one: {k1} vs {k12}. Zero means muscle_grade is not in the key "
        f"and a regrade is invisible to the census.")
    assert (k1[diffs[0]], k12[diffs[0]]) == (GRADE_1, GRADE_1_2), (
        f"the one position that moved holds {k1[diffs[0]]!r}/"
        f"{k12[diffs[0]]!r}, not the muscle grade")


def test_two_lots_differing_only_in_the_price_are_two_keys():
    """
    The third member of the family above, for the field a revision moves most
    often -- and the one the family was missing. Every component equal except
    the price, and the keys must still differ, in that one position.

    ONE CENT, AND THE CENT DRAWN FROM THE LIVE COLLISION SET, for the reason
    the test above gives: 275.01 * 100 lands just above 27501 and every
    implementation separates it from 275.00, while 256.03 * 100 is
    25602.999999999996 and a truncating price_cents() files it under 256.02's
    key. Both prices are stored in mars_sales -- see the live-database tests
    in the last section.

    MUTANT THAT MUST DIE: return int(float(Decimal(str(p))) * 100) from
    price_cents(). The superseded price and the correction then produce one
    key and the census can never see a one-cent revision.

    Stated as "these two differ AND they differ only here", like its two
    siblings, because "they differ" alone would also pass if the mutation had
    broken some other component instead.
    """
    assert ONE_LOT["avg_price"] == 330.29
    assert _key_for() == _key_for(avg_price=330.29), (
        "the control failed: this comparison is measuring something other "
        "than the price")

    low, high = _key_for(avg_price=256.02), _key_for(avg_price=256.03)
    assert low != high, (
        "a one-cent price correction produces one key for two different "
        "lots, so the census can never see the commonest revision AMS makes")
    diffs = _one_position_apart(low, high)
    assert len(diffs) == 1, (
        f"these two keys differ in components {diffs}, not in the price "
        f"alone: {low} vs {high}")
    assert (low[diffs[0]], high[diffs[0]]) == (25602, 25603), (
        f"the one position that moved holds {low[diffs[0]]!r}/"
        f"{high[diffs[0]]!r}, not the price in cents")


# ---------------------------------------------------------------------------
# report_date. The biggest of the three, and the one compare() advertises.
# ---------------------------------------------------------------------------

def test_two_lots_differing_only_in_the_report_date_are_two_keys():
    """
    The key-level statement, narrowest form: every component equal except the
    report date, and the keys must still differ.

    MUTANT THAT MUST DIE: drop _iso(report_date) from key_of()'s returned
    tuple.

    report_date is the FIRST component of mars_sales' primary key and the one
    with by far the most stored rows behind it -- 252 row-pairs in
    data/mars_history.db on 2026-09-30 agree on the other five and are kept
    apart by this one alone, against 5 for head_count and 4 for muscle_grade.
    Every one of those pairs becomes a single key if this component leaves,
    and a stored row whose key collides with a served row's is a row the
    census can never report.

    Stated as "these two differ AND they differ only here", following
    test_two_lots_differing_only_in_head_count_are_two_keys: "they differ"
    alone would also pass if the mutation had broken some other component
    instead. Both keys are built from ONE_LOT with a single field overridden,
    so "the keys differ" cannot be true because a second field drifted between
    two hand-written positional calls.
    """
    assert ONE_LOT["report_date"] == "2026-09-22"
    assert _key_for() == _key_for(report_date="2026-09-22"), (
        "the control failed: this comparison is measuring something other "
        "than the report date")

    tuesday, wednesday = (_key_for(report_date="2026-09-22"),
                          _key_for(report_date="2026-09-23"))
    assert tuesday != wednesday, (
        "report_date is not in the key, so a lot a narrative revision "
        "RE-DATES produces one key for two stored rows -- the double count "
        "compare()'s docstring claims to catch and the delete path could not")
    diffs = _one_position_apart(tuesday, wednesday)
    assert len(diffs) == 1, (
        f"these two keys differ in components {diffs}, not in the report date "
        f"alone: {tuesday} vs {wednesday}")
    assert (tuesday[diffs[0]], wednesday[diffs[0]]) == ("2026-09-22",
                                                        "2026-09-23"), (
        f"the one position that moved holds {tuesday[diffs[0]]!r}/"
        f"{wednesday[diffs[0]]!r}, not the report date")


def test_a_date_of_every_type_produces_one_key():
    """
    The other direction, and it is not decoration -- it is the reason key_of()
    runs the date through _iso() rather than storing it raw. The backends
    disagree: SQLite hands back a TEXT '2026-09-22', Snowflake's connector
    hands back a datetime.date, and a datetime with a time on it turns up
    wherever a published_date got carried along. Comparing those raw is how a
    key set silently fails to intersect, and an empty intersection reads as
    "AMS withdrew the whole day".

    MUTANT THAT MUST DIE: return report_date unnormalised.
    """
    from datetime import date, datetime

    iso = _key_for(report_date="2026-09-22")
    assert _key_for(report_date=date(2026, 9, 22)) == iso
    assert _key_for(report_date=datetime(2026, 9, 22, 6, 53)) == iso
    assert _key_for(report_date=" 2026-09-22 ") == iso

    # and normalisation must not collapse two real dates
    assert _key_for(report_date="2026-09-23") != iso


def test_a_revision_that_re_dates_a_lot_leaves_the_old_copy_as_a_phantom():
    """
    THE CAPABILITY compare()'s OWN DOCSTRING CLAIMS, and nothing in the suite
    held it: "a lot a narrative revision RE-DATES changes report_date and
    therefore changes its key, so the old copy surfaces as a phantom while the
    new copy sits beside it -- the double-count the delete path explicitly
    could not reach."

    THE EVENT, and it is the shape derived_dates() exists for rather than a
    hypothetical. detect_final_sale_day() moves a report FORWARD to the latest
    weekday its narrative names; El Reno OK's 9/1/26 report is the documented
    case, a Tuesday report whose narrative described sales on Tuesday AND
    Wednesday. McAlester (slug 1827) sells on Tuesdays -- 09-08, 09-15 and
    09-22 in the captured window, all of them. So a first publication dated
    Monday 09-21 with no narrative stores the lot under 09-21, and a revision
    whose narrative names Tuesday moves the SAME lot to 09-22. merge_ignore
    inserts the re-dated copy and cannot remove what it replaced, so we hold
    both and recompute_fci_daily() -- no WHERE clause -- counts both.

    MUTANT THAT MUST DIE: drop _iso(report_date) from key_of(). The old copy
    and the re-dated one then produce ONE key, the old copy matches the served
    set, and the census returns a clean page over a doubled lot.

    THE KEY ASSERTION ALONE IS WEAKER THAN THIS. A key that differs proves
    only that two tuples are not equal; it does not prove the comparison ever
    reaches them. So the same pair is driven through compare() against the
    captured payload, and the phantom has to be NAMED -- with its own old
    date on it, not the corrected one.
    """
    correction = next(r for r in McALESTER if r.weight_low == 750)
    assert (correction.report_date, correction.raw_date) == ("2026-09-22",
                                                             "2026-09-22")
    assert (correction.head_count, correction.avg_price) == (14, 330.29)

    # The pre-revision copy: the same lot, under the date AMS first gave it.
    # raw_date moves with report_date because derived_dates() derives one from
    # the other -- but raw_date is not a key component, so the KEY differs in
    # exactly one position and the assertion below says which.
    stale = _row("2026-09-21", "2026-09-21", 1827, "McAlester", 750,
                 correction.muscle_grade, correction.head_count,
                 correction.avg_weight, correction.avg_price)
    assert stale.key != correction.key, (
        "report_date is not in the key, so a re-dated lot is invisible to the "
        "census -- the old copy and the correction are one key and both sit "
        "in the index")
    diffs = _one_position_apart(stale.key, correction.key)
    assert len(diffs) == 1 and (stale.key[diffs[0]],
                                correction.key[diffs[0]]) == ("2026-09-21",
                                                              "2026-09-22"), (
        f"the two keys differ in components {diffs}, not in the report date "
        f"alone: {stale.key} vs {correction.key}")

    stored, payloads, locs = _slug_fixture(1827, extra=[stale])
    lo, hi = mc.judged_window(*WINDOW)
    assert lo <= mc._date(stale.raw_date) <= hi, (
        f"the re-dated copy's sale day {stale.raw_date} fell outside the "
        f"judged window {lo}..{hi}, so this test cannot see the key collapse")

    f = mc.compare(stored, payloads, *WINDOW, locations=locs)
    assert [(p.slug_id, p.raw_date, p.report_date, p.weight_low,
             p.muscle_grade, p.head_count, p.avg_price) for p in f.phantom] == [
        (1827, "2026-09-21", "2026-09-21", 750, "1", 14, 330.29)], (
        f"the superseded copy under the old report date was not named: "
        f"{[p.describe() for p in f.phantom]}")
    assert f.missing == (), [m.describe() for m in f.missing]
    assert f.withheld == () and f.notes == ()


# ---------------------------------------------------------------------------
# weight_low.
# ---------------------------------------------------------------------------

def test_two_lots_differing_only_in_the_weight_break_are_two_keys():
    """
    The same claim for the component that says which of CME's four brackets a
    lot belongs to. weight_low is in mars_sales' primary key and it is the
    field tests/test_index_isolation.py polices structurally -- mars_sales may
    hold 700, 750, 800 and 850 and nothing else -- so two lots that agree on
    everything but the bracket are two real lots on one real report.

    MUTANT THAT MUST DIE: drop int(weight_low) from key_of()'s returned tuple.
    25 stored row-pairs in data/mars_history.db on 2026-09-30 are kept apart
    by this component alone; every one of them collapses if it leaves.

    Both keys built from ONE_LOT with the one field overridden, so the
    difference cannot come from somewhere else.
    """
    assert ONE_LOT["weight_low"] == 750
    assert _key_for() == _key_for(weight_low=750), (
        "the control failed: this comparison is measuring something other "
        "than the weight break")

    light, heavy = _key_for(weight_low=750), _key_for(weight_low=800)
    assert light != heavy, (
        "weight_low is not in the key, so a lot refiled from the 750 bracket "
        "into the 800 one produces one key for two different lots and the "
        "census can never see it")
    diffs = _one_position_apart(light, heavy)
    assert len(diffs) == 1, (
        f"these two keys differ in components {diffs}, not in the weight "
        f"break alone: {light} vs {heavy}")
    assert (light[diffs[0]], heavy[diffs[0]]) == (750, 800), (
        f"the one position that moved holds {light[diffs[0]]!r}/"
        f"{heavy[diffs[0]]!r}, not the weight break")


def test_a_weight_break_of_every_type_produces_one_key():
    """
    The other direction, for the same reason head_count has one: AMS serves
    weight_break_low as a bare int, SQLite hands back an INTEGER and a backend
    round trip can turn it into a float or a string. int() is what stops
    '750', 750 and 750.0 being three brackets -- and three brackets where
    there is one is a whole sale day failing to intersect.
    """
    assert _key_for(weight_low="750") == _key_for(weight_low=750)
    assert _key_for(weight_low=750.0) == _key_for(weight_low=750)


def test_a_revision_that_only_moves_the_weight_break_is_still_a_phantom():
    """
    THE SAME EVENT AS THE head_count AND muscle_grade REVISION TESTS, for the
    third field a revision can move on its own. AMS really does refile a lot
    between the 700/750/800/850 brackets between a Preliminary and a Final --
    the head-count-only case those tests are built on was found on Oklahoma
    City's 2026-09-28 report, where the Preliminary carried 1,057 qualifying
    head and the Final 1,079, and a bracket move is the same class of
    re-sorting.

    We hold the superseded 750-bracket copy and AMS serves the lot at 800.
    merge_ignore inserted the correction and could not remove what it
    replaced, so both sit in mars_sales and recompute_fci_daily() counts both.

    MUTANT THAT MUST DIE: drop int(weight_low) from key_of(). The stale copy
    and the correction collapse to one key, the stale copy matches the served
    set, and the phantom is never reported.

    avg_weight IS DELIBERATELY LEFT AT THE CORRECTION'S VALUE, following
    test_the_real_mcalester_lot_reports_both_directions_too, which leaves it
    alone for the same reason: it is not a key component, and moving a second
    field would put two things in play in a test whose whole claim is about
    one. _one_position_apart() below is what says only one moved.
    """
    correction = next(r for r in McALESTER if r.weight_low == 750)
    assert (correction.head_count, correction.avg_price) == (14, 330.29)
    stale = _row(correction.report_date, correction.raw_date, 1827,
                 "McAlester", 800, correction.muscle_grade,
                 correction.head_count, correction.avg_weight,
                 correction.avg_price)
    assert stale.key != correction.key, (
        "weight_low is not in the key, so a lot refiled into another bracket "
        "is invisible to the census")
    diffs = _one_position_apart(stale.key, correction.key)
    assert len(diffs) == 1 and (stale.key[diffs[0]],
                                correction.key[diffs[0]]) == (800, 750), (
        f"the two keys differ in components {diffs}, not in the weight break "
        f"alone: {stale.key} vs {correction.key}")

    stored, payloads, locs = _slug_fixture(1827, extra=[stale])
    f = mc.compare(stored, payloads, *WINDOW, locations=locs)
    assert [(p.slug_id, p.raw_date, p.weight_low, p.muscle_grade,
             p.head_count, p.avg_price) for p in f.phantom] == [
        (1827, "2026-09-22", 800, "1", 14, 330.29)], (
        f"the superseded 750-bracket copy, refiled at 800, was not named: "
        f"{[p.describe() for p in f.phantom]}")
    assert f.missing == (), [m.describe() for m in f.missing]
    assert f.withheld == () and f.notes == ()


# ---------------------------------------------------------------------------
# slug_id. The sixth component, and the only one no consequence test can
# reach.
# ---------------------------------------------------------------------------

def test_two_lots_differing_only_in_the_slug_id_are_two_keys():
    """
    The sixth and last component of the tuple, pinned at the KEY LEVEL ONLY --
    and that limit is the interesting part of this test rather than an
    apology for it.

    NO compare()-LEVEL TEST OF THIS CAN EXIST TODAY, which is why it has none.
    compare() already scopes both sides by slug: keys_by_slug is built per
    slug_id and served_keys is rebuilt inside the per-slug loop, so two barns'
    keys are never in one set and slug_id inside the tuple is redundant to
    that comparison. Removing it from key_of() leaves the whole suite green,
    including every test above -- verified, not assumed -- and no phantom,
    missing or note changes anywhere. A consequence test asserting otherwise
    would be a test that cannot fail, which is the shape this file's own
    header warns about.

    SO WHY PIN IT. key_of()'s docstring says what it is: "mars_sales' PRIMARY
    KEY, normalised". The redundancy is a property of today's ONE caller, not
    of the key, and the next caller that pools keys across slugs -- a
    cross-slug dedupe, or a wide standalone sweep that builds one served set
    for a whole run -- would silently merge two barns' lots with no error.
    data/mars_history.db held 23 such row-pairs on 2026-09-30: two different
    slugs, one report date, one bracket, one grade, one price, one head count,
    kept apart by this component and nothing else.

    MUTANT THAT NOW DIES: drop int(slug_id) from key_of()'s returned tuple. It
    survived the tracked suite AND the rest of this file before this test
    existed.
    """
    assert ONE_LOT["slug_id"] == 1827
    assert _key_for() == _key_for(slug_id=1827), (
        "the control failed: this comparison is measuring something other "
        "than the slug id")

    mcalester, mitchell = _key_for(slug_id=1827), _key_for(slug_id=2022)
    assert mcalester != mitchell, (
        "slug_id is not in the key, so two barns' lots that agree on the "
        "date, the bracket, the grade, the price and the head count collapse "
        "into one key")
    diffs = _one_position_apart(mcalester, mitchell)
    assert len(diffs) == 1, (
        f"these two keys differ in components {diffs}, not in the slug id "
        f"alone: {mcalester} vs {mitchell}")
    assert (mcalester[diffs[0]], mitchell[diffs[0]]) == (1827, 2022), (
        f"the one position that moved holds {mcalester[diffs[0]]!r}/"
        f"{mitchell[diffs[0]]!r}, not the slug id")

    # and the same normalisation the other five components get
    assert _key_for(slug_id="1827") == _key_for(slug_id=1827)


# ---------------------------------------------------------------------------
# The .strip() on muscle_grade -- the one that fails the other way.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("padded", [" 1-2", "1-2 ", "  1-2  ", "\t1-2\n"])
def test_a_muscle_grade_that_arrived_padded_is_still_one_key(padded):
    """
    key_of() does str(muscle_grade).strip(), and the .strip() is not covered
    by test_grade_1_and_grade_1_2_differ_in_exactly_one_key_position -- that
    test says so in its own docstring, because both of its grades are
    already-stripped str and str().strip() is the identity on them. Its
    premise assertion is what makes it a test of the field's PRESENCE, and
    presence is all it can be.

    MUTANT THAT MUST DIE: str(muscle_grade) without the .strip().

    IT FAILS IN THE OPPOSITE DIRECTION TO THE TWO ABOVE, and that is the whole
    reason it is worth a test. Dropping report_date or weight_low HIDES a real
    phantom. Dropping the strip MANUFACTURES a false one out of whitespace:
    the stored side and the served side spell the same grade differently, the
    keys fail to intersect, and the census prints a line about a row that is
    perfectly fine. On a page whose entire product is that an empty result is
    the normal one, that is the failure that gets the real line ignored a week
    later.

    NO PADDED GRADE IS STORED TODAY -- mars_sales held 23,081 rows spelled '1'
    and 10,205 spelled '1-2' on 2026-09-30 and nothing else, checked
    read-only. That is the argument FOR the normalisation rather than against
    it: neither side is under our control, the two sides are keyed at
    different times by different code paths, and the cost of the spellings
    diverging once is not one row but a sale day that fails to intersect.
    """
    assert padded != padded.strip(), (
        f"{padded!r} carries no padding, so this case exercises nothing")
    assert _key_for(muscle_grade=padded) == _key_for(muscle_grade=GRADE_1_2), (
        f"a grade spelled {padded!r} on one side and {GRADE_1_2!r} on the "
        f"other produces two keys, so the same lot reads as a phantom AND as "
        f"missing -- a false line on a page whose value is that it is empty")


def test_stripping_the_grade_does_not_collapse_two_real_grades():
    """
    GUARD THE GUARD, and it is the direction the parametrised test above
    cannot reach: a normalisation loose enough to satisfy it could also be
    loose enough to merge '1' into '1-2'. CME Rule 10203 covers Medium & Large
    #1 AND #1-2, both are index-qualifying and both routinely appear on one
    report, so collapsing them would mask a withdrawal of either half.
    """
    assert _key_for(muscle_grade=" 1 ") != _key_for(muscle_grade=" 1-2 ")
    assert _key_for(muscle_grade=" 1 ") == _key_for(muscle_grade=GRADE_1)
    assert _key_for(muscle_grade=" 1-2 ") == _key_for(muscle_grade=GRADE_1_2)


def test_a_padded_stored_grade_does_not_manufacture_a_phantom():
    """
    THE SAME FIELD, DRIVEN THROUGH THE PRODUCTION KEY COMPOSITION AND THROUGH
    compare(), because a key-level equality says two tuples match and says
    nothing about whether a morning's page stays empty.

    THE EVENT. mars_sales holds a grade spelled with padding -- AMS served it
    that way on the day it was ingested, or a backend round trip added it --
    and AMS serves the same lot today spelled cleanly. Nothing is wrong: the
    lot is served, it qualifies, and the index counts it correctly. Without
    the .strip() the stored key and the served key are different tuples, so
    the SAME lot is reported twice over and under two opposite words: as a
    phantom we hold and AMS does not serve, and as a lot AMS serves and we do
    not hold. A human then goes looking for a row to delete that must not be
    deleted.

    MUTANT THAT MUST DIE: str(muscle_grade) without the .strip().

    DRIVEN THROUGH load_stored_groups() AGAINST A REAL mars_sales, never
    through _row(): _row() composes its own key, so the one production path
    that builds a STORED key would never be executed and the mutant this
    docstring names could live through it. That is the hole the Ericson
    docstring already names for report_date, where a mutant survived a full
    run.

    GUARD THE GUARD IS INLINE. A silent comparison proves nothing if the
    comparison never happened, so the same fixture is run a second time with
    AMS serving the OTHER grade, and the padded row must then be named. The
    silence above has to be the strip doing its job, not the row failing to
    arrive.
    """
    padded = f" {GRADE_1_2} "
    conn = _memory_db()
    conn.execute(
        "INSERT INTO mars_sales (report_date, raw_date, slug_id, location, "
        "state, weight_low, muscle_grade, head_count, avg_weight, avg_price) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("2026-09-22", "2026-09-22", 9001, "Testville", "TX", 750, padded, 14,
         772.0, 330.29))
    conn.commit()
    stored = mc.load_stored_groups(conn, [9001], *WINDOW)
    conn.close()

    held = stored[(9001, "2026-09-22")]
    assert [r.muscle_grade for r in held] == [padded], (
        f"the loader normalised the stored grade to "
        f"{[r.muscle_grade for r in held]}, so key_of()'s strip is no longer "
        f"what this test is measuring")

    # AMS serves the same lot, spelled its usual way. Nothing is wrong.
    f = mc.compare(stored, {9001: _payload([_served(grade=GRADE_1_2)])},
                   *WINDOW)
    assert f.phantom == (), (
        f"whitespace manufactured a phantom out of a lot AMS is serving: "
        f"{[p.describe() for p in f.phantom]}")
    assert f.missing == (), (
        f"the same whitespace reported the served lot as one we do not hold: "
        f"{[m.describe() for m in f.missing]}")
    assert f.notes == () and f.withheld == ()
    assert f.n_compared == 1, "the sale day was not compared, so this is silent for the wrong reason"

    # GUARD THE GUARD. AMS withdraws the #1-2 lot and serves a #1 one instead:
    # the padded row really is unserved now, and it must be named. Without
    # this, a comparison that had gone dark would pass the assertions above.
    withdrawn = mc.compare(stored, {9001: _payload([_served(grade=GRADE_1)])},
                           *WINDOW)
    assert [(p.slug_id, p.weight_low, p.muscle_grade.strip(), p.head_count,
             p.avg_price) for p in withdrawn.phantom] == [
        (9001, 750, GRADE_1_2, 14, 330.29)], (
        f"the comparison cannot name this row even when AMS stops serving "
        f"it, so its silence above says nothing: "
        f"{[p.describe() for p in withdrawn.phantom]}")


# ---------------------------------------------------------------------------
# One derivation, not two.
# ---------------------------------------------------------------------------

def test_derived_dates_reproduces_what_the_insert_path_stores():
    """
    Both dates, for a plain weekday row, a row whose narrative names a later
    weekday, and a Saturday row.
    """
    plain = {"report_date": "09/22/2026", "report_narrative": None}
    assert ui.derived_dates(plain) == ("2026-09-22", "2026-09-22")

    # El Reno's real shape: a Tuesday report whose narrative describes a
    # Wednesday sale. raw_date moves; report_date follows it.
    later = {"report_date": "09/01/2026",
             "report_narrative": "Feeder cattle sold on Wednesday."}
    assert ui.derived_dates(later) == ("2026-09-02", "2026-09-02")

    # Ericson's real shape: a Saturday sale filed under the Monday.
    saturday = {"report_date": "09/12/2026", "report_narrative": None}
    assert ui.derived_dates(saturday) == ("2026-09-14", "2026-09-12")


def _run_update_body():
    src = (REPO / "update_index.py").read_text(encoding="utf-8")
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name == "run_update":
            return node
    raise AssertionError("run_update() not found in update_index.py")


def _calls_in(node):
    return [ast.unparse(n.func) for n in ast.walk(node) if isinstance(n, ast.Call)]


def test_the_insert_path_calls_the_shared_derivation():
    """
    Read off the AST, not matched as a string, so a mention in a comment or a
    docstring cannot satisfy it. Two copies of this derivation would agree on
    the day they were written and drift the first time either rule moved.
    """
    calls = _calls_in(_run_update_body())
    assert "derived_dates" in calls, \
        "run_update() no longer calls derived_dates()"
    for banned in ("detect_final_sale_day", "shift_weekend_to_monday"):
        assert banned not in calls or banned == "shift_weekend_to_monday", banned
    assert "detect_final_sale_day" not in calls, (
        "run_update() composes the sale-day rule inline again; the census "
        "keys served rows through derived_dates() and the two would drift.")


def test_the_derivation_check_can_actually_fail():
    """Guard the guard: prove _calls_in sees a call and ignores a mention."""
    real = ast.parse("def f():\n    x = derived_dates(r)\n").body[0]
    prose = ast.parse('def f():\n    """calls derived_dates(r)"""\n    pass\n').body[0]
    assert "derived_dates" in _calls_in(real)
    assert "derived_dates" not in _calls_in(prose)


# ---------------------------------------------------------------------------
# The window, and where its 6 comes from.
# ---------------------------------------------------------------------------

def test_the_shift_cannot_exceed_the_window_guards_allowance():
    """
    MAX_DATE_SHIFT_DAYS is DERIVED, not chosen. detect_final_sale_day() moves a
    report forward to the latest weekday its narrative names, so its maximum
    reach is Monday -> Sunday. Walk every weekday against every named weekday
    and assert nothing moves further than the constant the window guard is
    built on.
    """
    weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                "Saturday", "Sunday"]
    worst = 0
    start = date(2026, 9, 14)                   # a Monday
    for i in range(7):
        d = start + timedelta(days=i)
        for name in weekdays:
            moved = ui.detect_final_sale_day(d, f"cattle sold on {name}")
            worst = max(worst, (moved - d).days)
            assert moved >= d, "the rule must never move a report backwards"
    assert worst == mc.MAX_DATE_SHIFT_DAYS, (
        f"detect_final_sale_day() can now move a date by {worst} days, but "
        f"judged_window() still allows {mc.MAX_DATE_SHIFT_DAYS}. A group "
        f"inside the gap would be judged against a report never asked for.")


def test_a_group_below_the_window_floor_is_not_judged():
    """
    A stored group at since+5 was produced by a report dated anywhere in
    [since-1, since+5], and since-1 was never queried -- so a report we never
    asked for would look exactly like a lot AMS withdrew.

    MUTANT THAT MUST DIE: lo = since.
    """
    since, until = date(2026, 9, 14), date(2026, 9, 28)
    payloads = {9999: _payload([_served(report_date="09/28/2026")])}
    for offset, judged in ((5, False), (6, True)):
        sale = (since + timedelta(days=offset)).isoformat()
        stored = {(9999, sale): (
            _row(sale, sale, 9999, "Testville", 750, "1", 9, 770.0, 300.00),)}
        f = mc.compare(stored, payloads, since, until)
        assert bool(f.phantom) is judged, (
            f"a group at since+{offset} was "
            f"{'not ' if judged else ''}judged, and should have been "
            f"{'' if judged else 'not '}")
    assert mc.judged_window(since, until) == (date(2026, 9, 20), until)


def test_the_missing_direction_is_bounded_by_the_same_judged_window():
    """
    BOTH DIRECTIONS USE ONE WINDOW. judged_window() says so and gives the
    reason: "The panel makes one statement about one window; a finding outside
    the window it claims to have judged is a finding nobody can place."

    The phantom direction's floor is covered by
    test_a_group_below_the_window_floor_is_not_judged(). This is its twin on
    the missing direction, and without it the guard can be deleted outright:
    the run then counts sale days it was never entitled to an opinion about
    into n_compared -- the number the panel prints as "sale day(s) checked
    against USDA" -- and files findings dated outside the window printed
    beside them.

    MUTANT THAT MUST DIE: drop the `lo <= _date(raw_iso) <= hi` guard from the
    missing loop in compare().
    """
    since, until = date(2026, 9, 14), date(2026, 9, 28)
    assert mc.judged_window(since, until) == (date(2026, 9, 20), until)

    below = {9999: _payload([_served(report_date="09/16/2026")])}
    f = mc.compare({}, below, since, until)
    assert f.missing == (), (
        "a sale day four days below the floor is not this run's to judge: "
        f"{[m.describe() for m in f.missing]}")
    assert f.n_compared == 0, (
        f"the panel would claim {f.n_compared} sale day(s) checked over "
        f"{f.window_start}..{f.window_end}, counting one outside it")

    # The direction itself still works one day inside the floor, and what it
    # reports is placeable on the window the same run prints.
    inside = {9999: _payload([_served(report_date="09/21/2026")])}
    g = mc.compare({}, inside, since, until)
    assert [m.raw_date for m in g.missing] == ["2026-09-21"]
    assert g.n_compared == 1
    for finding in g.rows:
        assert g.window_start <= finding.raw_date <= g.window_end, (
            f"{finding.describe()} sits outside the "
            f"{g.window_start}..{g.window_end} window the same run claims to "
            f"have judged")

    # And over the real 89-slug window the count stays the measured one: 126
    # sale days, every one inside the window the panel names.
    payloads, locations, _ = _load_payloads()
    h = mc.compare(_stored_rows(), payloads, *WINDOW, locations=locations,
                   roster_slugs=sorted(payloads))
    assert h.n_compared == 126, (
        f"the clean fixture compares 126 sale days; this run claims "
        f"{h.n_compared} over {h.window_start}..{h.window_end}")


# ---------------------------------------------------------------------------
# Payloads that cannot be believed.
# ---------------------------------------------------------------------------

def _at_stake():
    sale = "2026-09-22"
    return {(9999, sale): (
        _row(sale, sale, 9999, "Testville", 750, "1", 14, 772.0, 330.29),)}


@pytest.mark.parametrize("stats,why", [
    ({"returnedRows": 100, "totalRows": 178}, "100 of 178"),
    ({}, "completeness"),
    ({"returnedRows": 178}, "completeness"),
])
def test_an_incomplete_payload_withholds_instead_of_reporting_phantoms(stats, why):
    """
    A truncated report is indistinguishable from a withdrawal and would present
    as the whole sale day going phantom. AMS answers this directly -- every
    response carries stats.returnedRows and stats.totalRows.

    MUTANT THAT MUST DIE: hardcode complete = True.
    """
    payloads = {9999: _payload([_served(weight=1100)], stats=stats)}
    f = mc.compare(_at_stake(), payloads, *WINDOW)
    assert f.phantom == (), "an unbelievable payload has no opinion"
    assert len(f.withheld) == 1
    assert why in f.withheld[0].detail
    assert f.withheld[0].kind == mc.WITHHELD


def test_an_empty_payload_withholds_rather_than_condemning_the_day():
    """An error payload, or a report-family rename emptying every slug at once."""
    f = mc.compare(_at_stake(), {9999: _payload([])}, *WINDOW)
    assert f.phantom == ()
    assert len(f.withheld) == 1
    assert "no rows at all" in f.withheld[0].detail


def test_a_slug_whose_fetch_raised_is_withheld_and_not_passed_over():
    """
    THE ALL-CLEAR THAT COVERED UNCHECKED ROWS. update_index.py's roster loop
    drops a slug from census_payloads when its fetch raises. Scope was derived
    from that dict, so the slug was never judged, produced no finding of any
    kind, and the summary line then asserted "0 stored row(s) AMS no longer
    serves ... 0 slug(s) withheld" over rows nobody had looked at. A truncated
    payload was already withheld; an absent one is the same proposition.

    MUTANT THAT MUST DIE: derive eligibility from served_payloads alone
    (`eligible = set(served_payloads)`).
    """
    f = mc.compare(_at_stake(), {}, *WINDOW, roster_slugs=[9999])
    assert len(f.withheld) == 1, [w.describe() for w in f.withheld]
    assert f.withheld[0].kind == mc.WITHHELD
    assert f.withheld[0].slug_id == 9999
    assert "no payload arrived" in f.withheld[0].detail
    assert f.phantom == () and f.missing == ()
    assert f.n_compared == 0, "an unfetched slug's sale days were counted as compared"


def test_a_fetched_slug_is_judged_even_when_the_roster_no_longer_lists_it():
    """
    ELIGIBILITY IS A UNION, AND THIS IS THE HALF OF IT NOTHING ELSE COVERS.
    Every existing eligibility test runs the other way -- a roster slug with
    no payload -- and the roster alone satisfies all of them, so the union's
    own stated reason is untested: "the payloads are direct evidence that a
    slug WAS fetched and a roster edit must never silently stop a fetched slug
    from being judged."

    The shape that gets us: a roster edit drops slug 2022 -- a barn renamed, a
    line lost in a merge, a retype -- while the fetch loop still returns its
    payload, and the Mitchell phantom is sitting in mars_sales. On the roster
    alone that phantom VANISHES from the census and the summary line still
    reads "0 stored row(s) AMS no longer serves": the check went dark and
    printed reassurance, which is the worst available pair. Exactly the
    failure _eligible() exists for, arriving from the other side.

    MUTANT THAT MUST DIE: eligibility is the roster alone --
    `slugs = {int(k) for k in roster_slugs}` in _eligible().

    DRIVEN THROUGH compare() AND ASSERTED ON THE FINDINGS. _eligible() is
    private and its return value is not the product; a test that called it
    directly would keep passing if the call site stopped consulting it.
    """
    payloads, locations, _ = _load_payloads()
    stored = dict(_stored_rows())
    day = (2022, "2026-09-17")
    stored[day] = stored[day] + (MITCHELL_PHANTOM,)
    roster = [s for s in sorted(payloads) if s != 2022]      # the roster edit

    f = mc.compare(stored, payloads, *WINDOW, locations=locations,
                   roster_slugs=roster)
    assert [(p.slug_id, p.raw_date, p.weight_low, p.muscle_grade,
             p.head_count, p.avg_price) for p in f.phantom] == [
        (2022, "2026-09-17", 850, "1-2", 6, 275.00)], (
        "AMS served this slug and we hold a row it does not serve, but the "
        f"census reported {[p.describe() for p in f.phantom]}")
    assert "0 stored row(s) AMS no longer serves" not in mc.summary_line(f)

    # And the sale days it claims to have checked do not shrink either: both
    # Mitchell days are still in the count, so the count and the finding
    # cannot come to disagree about what was looked at.
    full = mc.compare(_stored_rows(), payloads, *WINDOW, locations=locations,
                      roster_slugs=sorted(payloads))
    assert f.n_compared == full.n_compared == 126, (
        f"the roster edit moved the sale days checked from {full.n_compared} "
        f"to {f.n_compared} -- a fetched slug stopped being judged and the "
        f"summary line did not say so")


def test_a_payload_that_over_returns_cannot_be_believed():
    """
    returnedRows == totalRows, NOT returnedRows >= totalRows. The module's
    preamble names this as one of only three numbers in the file: "An equality
    on a signal AMS supplies, not a tolerance on one we invented."

    An over-returning payload is not a harmless surplus. returnedRows > total
    means AMS's own two counts disagree -- a paging bug, a report-family
    change, a response assembled from two queries -- and a response whose
    accounting does not add up is precisely one we cannot subtract stored rows
    from. Believe it and the slug is judged rather than withheld, so whatever
    that response left out presents as the barn's stored rows going phantom.

    MUTANT THAT MUST DIE: `returned < total` in _disbelief(). The existing
    parametrize only ever passes returned < total, so the relaxed comparison
    keeps every one of those withheld and the suite stays green.
    """
    payloads = {9999: _payload([_served(weight=1100)],
                               stats={"returnedRows": 200, "totalRows": 178})}
    f = mc.compare(_at_stake(), payloads, *WINDOW)
    assert f.phantom == (), (
        "a payload whose own two counts disagree has no opinion about what "
        f"AMS serves: {[p.describe() for p in f.phantom]}")
    assert len(f.withheld) == 1
    assert f.withheld[0].kind == mc.WITHHELD
    assert "200 of 178" in f.withheld[0].detail


def test_the_mitchell_phantom_cannot_hide_behind_a_failed_fetch():
    """
    The real shape, end to end and through the tables the page reads: the
    Mitchell phantom in mars_sales and slug 2022's fetch raising. The line the
    dashboard prints must not be an all-clear.
    """
    import mars_census_view as view

    payloads, locations, _ = _load_payloads()
    conn = _memory_db()
    for r in [x for rs in _stored_rows().values() for x in rs] + [MITCHELL_PHANTOM]:
        conn.execute("INSERT INTO mars_sales (report_date, raw_date, slug_id, "
                     "location, state, weight_low, muscle_grade, head_count, "
                     "avg_weight, avg_price) VALUES (?,?,?,?,'XX',?,?,?,?,?)",
                     (r.report_date, r.raw_date, r.slug_id, r.location,
                      r.weight_low, r.muscle_grade, r.head_count,
                      r.avg_weight, r.avg_price))
    conn.commit()

    served = {s: p for s, p in payloads.items() if s != 2022}
    f = mc.run_census(conn, served, *WINDOW, locations=locations,
                      roster_slugs=sorted(payloads))
    assert [w.slug_id for w in f.withheld] == [2022], \
        [w.describe() for w in f.withheld]
    assert "0 slug(s) withheld" not in mc.summary_line(f)

    state, summary, rows = view.census_state(conn)
    assert state == view.FINDINGS
    assert int(summary["n_withheld"]) == 1
    line = view.headline(state, summary)
    assert "no discrepancies" not in line
    assert "could not be checked" in line
    conn.close()


def test_the_run_still_reads_clean_when_every_roster_slug_was_fetched():
    """
    The other direction, and the one that keeps the panel worth reading: the
    roster arriving in full must not add a line. Without this the fix above
    could be "withhold everything", which is silence by another route.
    """
    payloads, locations, _ = _load_payloads()
    f = mc.compare(_stored_rows(), payloads, *WINDOW, locations=locations,
                   roster_slugs=sorted(payloads))
    assert f.withheld == (), [w.describe() for w in f.withheld]
    assert f.phantom == () and f.missing == ()


def test_the_call_site_hands_the_census_the_roster():
    """
    THE ARGUMENT IS NOT THE GUARD UNLESS IT IS PASSED. compare() can be as
    roster-aware as it likes; if run_update() never supplies one, eligibility
    falls back to the payloads and the all-clear returns. Read off the AST so
    a mention in a comment cannot satisfy it.
    """
    body = _run_update_body()
    calls = [n for n in ast.walk(body) if isinstance(n, ast.Call)
             and ast.unparse(n.func) == "write_census"]
    assert len(calls) == 1, calls
    passed = ([ast.unparse(a) for a in calls[0].args]
              + [ast.unparse(k.value) for k in calls[0].keywords])
    assert "census_roster" in passed, (
        f"run_update() calls write_census({', '.join(passed)}) -- without the "
        f"roster a slug whose fetch raised is invisible to the census.")

    # and the roster it passes is the ROSTER, not the payload keys
    src = ast.unparse(body)
    assert "census_roster = [int(loc['slug_id']) for loc in roster]" in src, \
        "census_roster is no longer built from the roster itself"


def test_write_census_forwards_the_roster(monkeypatch, tmp_path):
    """The other half of the wiring: write_census must pass it on."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    seen = {}

    def _spy(conn, payloads, since, until, locations=None, roster_slugs=None):
        seen["roster"] = roster_slugs
        return mc.Findings("2026-09-20", "2026-09-28", 0)

    monkeypatch.setattr(mc, "run_census", _spy)
    ui.write_census({}, date(2026, 9, 14), date(2026, 9, 28), {}, [1853, 2022])
    assert seen["roster"] == [1853, 2022]


def test_withheld_is_scoped_to_what_is_actually_at_stake():
    """
    NOT A THRESHOLD -- "nothing is at stake". Measured 2026-09-28: 10 of 89
    slugs returned an empty payload and every one holds ZERO rows in the judged
    window. They are barns that did not sell. Reporting them would put ten
    noise lines on the page every morning, and noise gets ignored.
    """
    empty = {9999: _payload([])}
    assert mc.compare({}, empty, *WINDOW).withheld == ()
    assert len(mc.compare(_at_stake(), empty, *WINDOW).withheld) == 1

    # and the same scoping for a slug whose fetch raised: a roster barn we
    # hold nothing for in the judged window is not a line on the page.
    assert mc.compare({}, {}, *WINDOW, roster_slugs=[9999]).withheld == ()
    assert len(mc.compare(_at_stake(), {}, *WINDOW,
                          roster_slugs=[9999]).withheld) == 1


def test_an_unkeyable_served_row_annotates_rather_than_suppresses():
    """
    The delete path made this a hard refusal. For a detector that is an
    overreaction: there is no loss to prevent, and a false line costs a human
    one minute. So the count rides along on the finding and nothing is hidden.
    """
    payloads = {9999: _payload([
        _served(weight=1100, price=None),                 # un-keyable, not index-shaped
        _served(weight=800, head=3, price=310.0),         # a real served lot
    ])}
    f = mc.compare(_at_stake(), payloads, *WINDOW)
    assert len(f.phantom) == 1
    assert "could not be keyed" in f.phantom[0].detail


# ---------------------------------------------------------------------------
# Scope: what may enter the comparison at all.
# ---------------------------------------------------------------------------

def test_merge_only_sources_can_never_be_judged():
    """
    Direct-trade and video reports are PDFs serving the CURRENT WEEK ONLY, with
    no historical-date parameter -- for any week but this one, "AMS withdrew
    it" and "we did not ask" are the same observation. They live in mars_sales
    like everything else, so eligibility is derived from the FETCH rather than
    from SELECT DISTINCT slug_id, which would hand exactly those to the census.

    This was a runtime assertion in the reverted delete path; it is a test
    here. The two families share one 4-digit AMS numbering space -- slug 3102
    is Apache OK, a video report, four digits from roster entries on both
    sides -- so a future roster edit really could collide.
    """
    from direct_reports import DIRECT_REPORT_SLUGS
    from video_reports import VIDEO_REPORT_SLUGS

    roster = {int(loc["slug_id"]) for loc in
              json.loads(ui.ROSTER_PATH.read_text(encoding="utf-8"))}
    merge_only = set(DIRECT_REPORT_SLUGS.values()) | set(VIDEO_REPORT_SLUGS.values())
    assert not (roster & merge_only), sorted(roster & merge_only)


def test_only_fetched_slugs_are_judged():
    """
    A slug whose fetch RAISED is not a key in the payload map, so no group of
    it is JUDGED -- it can only ever be withheld, never condemned. That is
    structure, not a guard, and this is the test that keeps it structural.
    """
    stored = _at_stake()
    stored[(8888, "2026-09-22")] = (
        _row("2026-09-22", "2026-09-22", 8888, "Unfetched", 800, "1", 5, 810.0, 320.0),)

    f = mc.compare(stored, {9999: _payload([_served()])}, *WINDOW)
    assert all(p.slug_id != 8888 for p in f.phantom), \
        "a slug that was never fetched was judged absent"
    assert f.withheld == (), "a slug outside the roster is not this run's business"

    # On the roster and unfetched it is still never judged -- only withheld.
    f = mc.compare(stored, {9999: _payload([_served()])}, *WINDOW,
                   roster_slugs=[8888, 9999])
    assert all(p.slug_id != 8888 for p in f.phantom), \
        "a slug that was never fetched was judged absent"
    assert [w.slug_id for w in f.withheld] == [8888]


def test_the_census_run_before_the_merge_loop_would_report_the_whole_day():
    """
    ORDERING IS LOAD-BEARING, and this is why the real call site is after the
    ingest. With nothing stored, every qualifying served row reads as missing.
    Run before the merge loop, that is what every new row of the day would
    look like -- so the same column that means "merge_ignore declined this"
    after the loop would mean "we have not inserted it yet" before it.
    """
    payloads = {9999: _payload([_served(), _served(weight=800, head=3)])}
    assert len(mc.compare({}, payloads, *WINDOW).missing) == 2


def test_the_census_call_site_comes_after_the_merge_loop():
    """The source order itself, so the property above cannot silently invert."""
    body = _run_update_body()
    merges = [n.lineno for n in ast.walk(body) if isinstance(n, ast.Call)
              and ast.unparse(n.func).endswith("merge_ignore")]
    census = [n.lineno for n in ast.walk(body) if isinstance(n, ast.Call)
              and ast.unparse(n.func) == "write_census"]
    assert merges and census, (merges, census)
    assert min(census) > max(merges), (
        "write_census() runs before the last merge_ignore(); every row the run "
        "is about to insert would read as missing.")


# ---------------------------------------------------------------------------
# It cannot fail the run.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stub,label", [
    (lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")), "raises"),
    (lambda *a, **k: None, "returns None"),
    (lambda *a, **k: (x for x in []), "returns a generator"),
])
def test_a_broken_census_cannot_take_down_the_run(stub, label, monkeypatch,
                                                  capsys, tmp_path):
    """
    BOTH SIDES OF THE GUARD. barn_report's lesson was that the iteration over
    the result used to sit outside any try, so a stubbed report took the whole
    run to exit 1 -- after the index was computed and before it was pushed. A
    None and a generator both survive the call and die in the loop, which is
    exactly the shape that got through last time.
    """
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(mc, "run_census", stub)
    ui.write_census({}, date(2026, 9, 14), date(2026, 9, 28), {}, [])
    out = capsys.readouterr().out
    assert "AMS census skipped" in out, \
        f"a census that {label} was not caught: {out!r}"


def test_the_census_import_is_inside_the_guard():
    """
    A print-only diagnostic imported at module scope took down the whole ingest
    once already: update_index.py imported barn_report at the top, so a syntax
    error in the report killed the run before a single row was fetched.

    MUTANT THAT MUST DIE: move `import mars_census` to module scope.
    """
    tree = ast.parse((REPO / "update_index.py").read_text(encoding="utf-8"))
    for node in tree.body:                       # module level only
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] + [getattr(node, "module", "")]
            assert not any("mars_census" in (n or "") for n in names), (
                "update_index.py imports mars_census at module scope; a syntax "
                "error in a report-only diagnostic would take down the ingest "
                "before a row is fetched.")


def test_a_census_module_that_will_not_import_cannot_take_down_the_run(
        monkeypatch, capsys):
    """
    The import failure itself, not just its position. Poisoning sys.modules is
    how a syntax error in mars_census.py actually reaches update_index.
    """
    monkeypatch.setitem(sys.modules, "mars_census", None)
    ui.write_census({}, date(2026, 9, 14), date(2026, 9, 28), {}, [])
    assert "AMS census skipped" in capsys.readouterr().out


def test_a_hanging_census_cannot_strand_a_finished_index(monkeypatch, capsys,
                                                         tmp_path):
    """
    A TRY/EXCEPT CANNOT CATCH A HANG, and a hang here is worse than a crash.
    scripts/daily_update.ps1 pushes as a LATER step and waits on this process,
    so a census that never returns means update_index.py never exits and a
    finished index is never published. Reproduced through the real call site:
    run_census() made to sleep, EXIT=124, "Recomputed FCI ... for 950 dates"
    printed and the push never reached.

    MUTANT THAT MUST DIE: join() with no timeout, or drop the deadline.
    """
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(ui, "CENSUS_DEADLINE_SECONDS", 0.5)
    entered = threading.Event()
    released = threading.Event()

    def _hang(*a, **k):
        entered.set()
        released.wait(60)               # released in the finally, not leaked

    monkeypatch.setattr(mc, "run_census", _hang)
    try:
        t0 = time.monotonic()
        ui.write_census({}, date(2026, 9, 14), date(2026, 9, 28), {}, [])
        waited = time.monotonic() - t0
    finally:
        released.set()

    assert entered.wait(5), "the census never started, so nothing was bounded"
    assert waited < 30, f"write_census() waited {waited:.1f}s on a hung census"
    assert "did not finish" in capsys.readouterr().out


def test_an_abandoned_census_leaves_the_previous_statement_and_says_so(
        monkeypatch, capsys, tmp_path):
    """
    THE OVERRUN LINE MUST DESCRIBE THE STATE IT ACTUALLY LEAVES. Its first
    version promised "the reconciliation panel will read 'unavailable'", on
    the reasoning that run_census() truncates before it inserts, so a
    half-finished one leaves mars_census_runs empty.

    It does not. The abandoned thread never reaches conn.commit(), so SQLite
    rolls the truncate back with everything else and BOTH TABLES KEEP THE
    PREVIOUS RUN'S CONTENTS -- the same thing a census that RAISES leaves,
    which is the already-accepted path. An operator line that names the wrong
    state is the failure shape this repository keeps getting bitten by, so
    both halves are pinned: what is in the tables, and what the line says.

    MUTANT THAT MUST DIE: tell the operator the panel will read 'unavailable'.
    """
    import mars_census_view as view

    dbfile = tmp_path / "t.db"
    monkeypatch.setattr(db, "DB_PATH", dbfile)
    monkeypatch.setattr(ui, "CENSUS_DEADLINE_SECONDS", 0.5)

    # A first, ordinary census, so there IS a previous statement to survive.
    seed = sqlite3.connect(dbfile)
    seed.execute("""CREATE TABLE mars_sales (
        report_date TEXT, raw_date TEXT, slug_id INTEGER, location TEXT,
        state TEXT, weight_low INTEGER, muscle_grade TEXT, head_count INTEGER,
        avg_weight REAL, avg_price REAL, published_date TEXT)""")
    seed.commit()
    mc.run_census(seed, {9999: _payload([_served()])}, *WINDOW,
                  locations={9999: "Testville"})
    before = seed.execute("SELECT run_at, n_compared FROM mars_census_runs").fetchall()
    seed.close()
    assert len(before) == 1, before

    released = threading.Event()

    def _half(conn, payloads, since, until, locations=None, roster_slugs=None):
        """Truncate, insert, and never commit -- the abandoned write."""
        mc.init_tables(conn)
        db.truncate(conn, "mars_census")
        db.truncate(conn, "mars_census_runs")
        conn.cursor().execute(
            "INSERT INTO mars_census_runs (run_at, window_start, window_end, "
            "n_compared, n_phantom, n_missing, n_withheld) "
            f"VALUES ({db.placeholders(7)})",
            ("HALF-WRITTEN", "2026-09-20", "2026-09-28", 1, 0, 0, 0))
        released.wait(60)                   # released in the finally, not leaked

    monkeypatch.setattr(mc, "run_census", _half)
    try:
        ui.write_census({}, date(2026, 9, 14), date(2026, 9, 28), {}, [])
        out = capsys.readouterr().out
        assert "did not finish" in out

        # The tables, read the way the next process reads them.
        check = sqlite3.connect(dbfile)
        after = check.execute(
            "SELECT run_at, n_compared FROM mars_census_runs").fetchall()
        state, summary, _ = view.census_state(check)
        check.close()
    finally:
        released.set()

    assert after == before, (
        f"an abandoned census changed the tables: {before} -> {after}")
    assert state != view.UNAVAILABLE, (
        "the overrun path does NOT leave an empty runs table; if it ever "
        "does, the wording below is what has to change with it")

    # and the operator line must say that, not the opposite
    assert "unavailable" not in out, (
        "the overrun line promises the panel will read 'unavailable', and it "
        "will not -- it shows the previous run's result.")
    assert "PREVIOUS run" in out and "stamped" in out


def test_the_deadline_does_not_fire_on_a_census_that_finishes(monkeypatch,
                                                              capsys, tmp_path):
    """
    GUARD THE GUARD. A deadline that fires every run would 'pass' the test
    above while destroying the census, so the ordinary path has to be asserted
    too: the report is printed and the overrun line is not.
    """
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(
        mc, "run_census",
        lambda *a, **k: mc.Findings("2026-09-20", "2026-09-28", 7))
    ui.write_census({}, date(2026, 9, 14), date(2026, 9, 28), {}, [])
    out = capsys.readouterr().out
    assert "7 sale day(s)" in out
    assert "did not finish" not in out
    assert "AMS census skipped" not in out


# The child process for the test below: a real write_census() whose census
# never returns. It prints the marker and then simply ends, so what is under
# test is whether the INTERPRETER can exit with the worker still running.
_STALLED_RUN = '''\
import sys, threading
sys.path.insert(0, {repo!r})
from datetime import date
import snowflake_db as db
import mars_census as mc
import update_index as ui

db.DB_PATH = {dbfile!r}
ui.CENSUS_DEADLINE_SECONDS = 0.5
mc.run_census = lambda *a, **k: threading.Event().wait({block})

ui.write_census({{}}, date(2026, 9, 14), date(2026, 9, 28), {{}}, [])
print("RUN CONTINUED PAST THE CENSUS", flush=True)
'''


def test_the_census_thread_is_a_daemon_so_a_stall_cannot_hold_the_run_open(
        monkeypatch, tmp_path):
    """
    daemon=True IS THE LOAD-BEARING HALF OF THE HANG FIX, and the deadline is
    the half that gets all the attention. join(CENSUS_DEADLINE_SECONDS) only
    returns control to the main thread; it does not end the worker. A
    NON-daemon worker still running at that point is joined again by the
    interpreter at shutdown, and Python waits for it there with no timeout at
    all -- so update_index.py never exits, scripts/daily_update.ps1 never gets
    past its Start-Process -Wait, the push never runs, and a computed index
    sits in SQLite unpublished. That is the exact failure this whole call site
    exists to prevent, and it was demonstrated before the fix: EXIT=124, the
    index recomputed, and the line after the census never printed.

    Remove daemon=True and every other test in this file still passes,
    including the one above it, because they all end the moment write_census()
    returns and the damage happens after that.

    MUTANT THAT MUST DIE: threading.Thread(...) without daemon=True.

    BOTH HALVES ARE ASSERTED. The flag, read off the real worker from inside
    the real worker; and the consequence, in a child process that is required
    to EXIT while its census is still stuck.
    """
    # --- the flag, on the thread write_census() actually creates -----------
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "flag.db")
    assert threading.current_thread().daemon is False, (
        "this test is running on a daemon thread, so the worker would inherit "
        "daemon=True and the mutant would survive -- the premise has to hold")

    seen = {}

    def _record(*a, **k):
        t = threading.current_thread()
        seen.update(daemon=t.daemon, name=t.name)
        return mc.Findings("2026-09-20", "2026-09-28", 0)

    monkeypatch.setattr(mc, "run_census", _record)
    ui.write_census({}, date(2026, 9, 14), date(2026, 9, 28), {}, [])

    assert seen, "the census never ran, so nothing about its thread was read"
    assert seen["name"] != "MainThread", (
        "the census ran on the main thread; a stall there cannot be abandoned "
        "at all")
    assert seen["daemon"] is True, (
        "the census worker is not a daemon. join() bounds the WAIT, not the "
        "thread, so a stalled census would be joined again at interpreter "
        "shutdown and hold the process open past the push.")

    # --- and the consequence, in a process that has to be able to die ------
    child = tmp_path / "stalled_run.py"
    child.write_text(_STALLED_RUN.format(
        repo=str(REPO), dbfile=str(tmp_path / "child.db"), block=120),
        encoding="utf-8")

    t0 = time.monotonic()
    try:
        r = subprocess.run([sys.executable, str(child)], capture_output=True,
                           text=True, timeout=30)
    except subprocess.TimeoutExpired as e:
        # e.stdout is str under text=True and bytes without it, and getting
        # that wrong here would replace the message below with an
        # AttributeError -- a test that fails for a reason nobody can read.
        so = e.stdout or ""
        raise AssertionError(
            "a run whose census stalled never exited. write_census() returned "
            "and the index was finished, but the interpreter is still waiting "
            "on the census thread at shutdown, so the push never happens. "
            "Output so far: "
            f"{(so.decode(errors='replace') if isinstance(so, bytes) else so)!r}"
        ) from None
    waited = time.monotonic() - t0

    assert waited < 20, (
        f"the child took {waited:.1f}s to exit; the stalled census is still "
        f"holding it open, just not forever")
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "did not finish" in r.stdout, r.stdout
    assert "RUN CONTINUED PAST THE CENSUS" in r.stdout, (
        f"the run did not get past the census at all: {r.stdout!r} {r.stderr!r}")


def _run_census_watching_the_thread(monkeypatch, tmp_path, stub=None):
    """
    Call the real write_census() with a recording threading.Thread in place.

    The recorder is a SUBCLASS of the real Thread, not a stand-in for one, so
    the worker still runs, still gets its own connection and still comes back
    through the real join. What is captured is the construction kwargs, the
    live thread object, and every timeout join() was called with -- so the
    two tests below read the thread update_index.py actually constructs and
    the timeout it actually joins on, never one the test built itself.

    The census is stubbed to return immediately: this is about the thread, and
    a stub keeps both tests to a few milliseconds with no sleeps. db.DB_PATH
    is redirected at tmp_path so _census_worker()'s get_conn() cannot reach
    data/mars_history.db.
    """
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")

    built, joins, ran = [], [], []

    class _Recording(threading.Thread):
        def __init__(self, *a, **kw):
            built.append({"args": a, "kwargs": dict(kw), "thread": self})
            super().__init__(*a, **kw)

        def join(self, timeout=None):
            joins.append(timeout)
            return super().join(timeout)

    def _census(*a, **k):
        ran.append(threading.current_thread())
        return mc.Findings("2026-09-20", "2026-09-28", 0)

    monkeypatch.setattr(mc, "run_census", stub or _census)
    # ui does `import threading`, so ui.threading IS the threading module;
    # monkeypatch puts the real class back when the test ends.
    monkeypatch.setattr(ui.threading, "Thread", _Recording)
    ui.write_census({}, date(2026, 9, 14), date(2026, 9, 28), {}, [])

    assert len(built) == 1, (
        f"write_census() built {len(built)} threads, not one -- these "
        f"assertions are about a single worker and no longer know which one "
        f"they are reading")
    return built[0], joins, ran


def test_the_worker_write_census_builds_is_created_as_a_daemon(monkeypatch,
                                                               tmp_path):
    """
    THE SAME FLAG AS THE TEST ABOVE, READ OFF THE CONSTRUCTION RATHER THAN
    FROM INSIDE THE WORKER, and the two fail on different mutants. That one
    reads current_thread().daemon from inside the census and pairs it with a
    child process that must exit; this one wraps the real threading.Thread
    for the duration of the real write_census() call.

    BOTH THE KWARG AND ITS EFFECT. The construction kwarg catches the line
    being deleted; the live thread's own .daemon catches it being undone
    afterwards (`worker.daemon = False`), which the kwarg alone would miss.

    THE PREMISE IS ASSERTED, NOT ASSUMED. A thread inherits daemon from the
    thread that creates it, so if pytest ever ran this on a daemon thread the
    worker would come out daemon=True with the flag deleted and the mutant
    would survive a test that still passed.

    MUTANT THAT MUST DIE: threading.Thread(...) without daemon=True, or with
    daemon=False, or with worker.daemon = False after construction.
    """
    assert threading.current_thread().daemon is False, (
        "this test is running on a daemon thread, so the worker would inherit "
        "daemon=True and the mutant would survive -- the premise has to hold")

    rec, _, ran = _run_census_watching_the_thread(monkeypatch, tmp_path)

    assert ran, "the census never ran, so nothing about its thread was read"
    assert ran[0] is rec["thread"], (
        "the census did not run on the thread write_census() built")
    assert rec["kwargs"].get("target") is ui._census_worker, (
        f"the one thread write_census() built does not run the census: "
        f"{rec['kwargs'].get('target')!r}")

    assert rec["kwargs"].get("daemon") is True, (
        f"write_census() built the census worker with daemon="
        f"{rec['kwargs'].get('daemon')!r}. join() bounds the WAIT, not the "
        f"thread: a stalled non-daemon census is joined again at interpreter "
        f"shutdown with no timeout, so update_index.py never exits and the "
        f"push never runs.")
    assert rec["thread"].daemon is True, (
        "the census worker was constructed as a daemon and is not one by the "
        "time it runs")


def test_the_census_join_is_bounded_by_the_deadline(monkeypatch, tmp_path):
    """
    THE OTHER HALF, READ OFF THE SAME CALL. worker.join() and
    worker.join(None) both block until the census returns, which is the
    unbounded wait the deadline exists to remove, and both leave every
    finding, every printed line and every exit code exactly as they are on a
    census that finishes -- so only a census that hangs would ever show it,
    and by then it has shown it in production.

    The hanging-census test above kills that mutant too, but only by waiting
    out a 60-second block. This one pins the timeout actually handed to
    join(), and does it in milliseconds.

    Asserted against the module constant rather than against 120, so raising
    or lowering the deadline is not a test edit; the value itself is judged in
    the test below.

    MUTANT THAT MUST DIE: worker.join() or worker.join(None) in place of
    worker.join(CENSUS_DEADLINE_SECONDS).
    """
    _, joins, ran = _run_census_watching_the_thread(monkeypatch, tmp_path)

    assert ran, "the census never ran, so the join proves nothing"
    assert joins, "write_census() never joined the census worker at all"
    assert joins[0] is not None, (
        "write_census() joins the census worker with no timeout. That is an "
        "unbounded wait on a diagnostic, on the critical path of a finished "
        "index that has not been pushed yet.")
    assert joins[0] == ui.CENSUS_DEADLINE_SECONDS, (
        f"the census join waits {joins[0]!r}, not CENSUS_DEADLINE_SECONDS "
        f"({ui.CENSUS_DEADLINE_SECONDS!r})")


def test_the_shipped_deadline_is_a_finite_positive_bound():
    """
    THE VALUE ITSELF, BECAUSE NOTHING ELSE IN THIS FILE RUNS IT. Every test of
    the overrun path has to monkeypatch CENSUS_DEADLINE_SECONDS down to 0.5s
    to be fast, so the number update_index.py actually ships with is executed
    by no other test in the suite. Set it to None and join(None) blocks
    forever -- the precise unbounded wait this call site was built to remove,
    with the daemon flag, the deadline line and the overrun message all still
    present and every other test still green. That mutant was run against the
    48-test file and survived the whole suite.

    BOUNDED ABOVE AS WELL AS BELOW, because a deadline is only a deadline if
    the run cannot outlive the thing waiting for it. daily_update.ps1 runs at
    07:45 and again at 13:00; a census measured at about one second must not
    be allowed to hold the push past the next scheduled run, so anything on
    the order of hours is a deadline in name only.

    AND BELOW, because a deadline shorter than the census is the opposite
    failure: it fires on every ordinary morning, the census is abandoned each
    time, and the reconciliation panel quietly stops being about today.

    MUTANT THAT MUST DIE: CENSUS_DEADLINE_SECONDS = None (or inf, or 0).
    """
    d = ui.CENSUS_DEADLINE_SECONDS

    assert isinstance(d, (int, float)) and not isinstance(d, bool), (
        f"CENSUS_DEADLINE_SECONDS is {d!r}. Thread.join() treats anything "
        f"that is not a number as 'no timeout' and waits forever.")
    assert math.isfinite(d), (
        f"CENSUS_DEADLINE_SECONDS is {d!r}, which is not a bound")
    assert d >= 10, (
        f"CENSUS_DEADLINE_SECONDS is {d!r}. The census takes about a second, "
        f"so a deadline this short abandons it on ordinary mornings and the "
        f"reconciliation panel stops describing today's run.")
    assert d <= 900, (
        f"CENSUS_DEADLINE_SECONDS is {d!r}. The index is computed and "
        f"committed but NOT pushed while this wait runs, and the next "
        f"scheduled run is hours away, not days -- a bound this large is one "
        f"in name only.")


# ---------------------------------------------------------------------------
# The log says which direction it means.
# ---------------------------------------------------------------------------

def _sections(lines):
    """
    report_lines() as {heading: [body lines]}. A heading is indented two
    spaces and a body line four or more, which is the shape report_lines()
    emits; lines[0] is the summary and belongs to no heading.
    """
    out, cur = {}, None
    for line in lines[1:]:
        if line.startswith("  ") and not line.startswith("   "):
            cur = line
            assert cur not in out, f"duplicate heading: {cur!r}"
            out[cur] = []
        else:
            assert cur is not None, f"body line before any heading: {line!r}"
            out[cur].append(line)
    return out


# One slug id per bucket, used nowhere else in this section, so "which bucket
# did this line come from" has exactly one answer.
PHANTOM_SLUG, MISSING_SLUG, WITHHELD_SLUG, NOTE_SLUG = 2022, 1827, 3333, 4444


def _lopsided_findings(n_phantom=1, n_missing=2, n_withheld=3, n_notes=4):
    """
    A Findings whose four buckets hold DIFFERENT, non-zero, UNEQUAL numbers of
    findings, each bucket's members carrying a slug id unique to that bucket.

    Unequal on purpose, and it is the whole point of the fixture. Equal counts
    make a swap arithmetically invisible: swap two buckets that both hold one
    finding and summary_line() renders the same string it rendered before, so
    an assertion over that string cannot fail and the test that "covers" the
    swap covers nothing. Non-zero for the same reason -- 0 and 0 are equal.

    The defaults for n_phantom=1 are MITCHELL_PHANTOM's own values and for
    n_notes=1 the Noteville row, so the callers below share one fixture rather
    than two that happen to agree.
    """
    phantom = tuple(
        mc._finding(
            mc.PHANTOM,
            _row("2026-09-17", "2026-09-17", PHANTOM_SLUG, "Mitchell",
                 850, "1-2", 6 + i, 853.0, 275.00),
            "held, not served")
        for i in range(n_phantom))
    missing = tuple(
        mc.Finding(kind=mc.MISSING, slug_id=MISSING_SLUG, location="McAlester",
                   raw_date="2026-09-22", report_date="2026-09-22",
                   index_date="2026-09-22", weight_low=700 + 50 * i,
                   muscle_grade="1", head_count=14, avg_weight=772.0,
                   avg_price=330.29, detail="served, not held")
        for i in range(n_missing))
    withheld = tuple(
        mc.Finding(kind=mc.WITHHELD, slug_id=WITHHELD_SLUG,
                   location=f"Withheldville {i}", detail=mc.NOT_FETCHED)
        for i in range(n_withheld))
    notes = tuple(
        mc._finding(
            mc.NOTE,
            _row("2026-09-22", "2026-09-22", NOTE_SLUG, "Noteville",
                 800, "1", 9 + i, 820.0, 300.00),
            "served but no longer qualifying")
        for i in range(n_notes))
    return mc.Findings(window_start="2026-09-11", window_end="2026-09-28",
                       n_compared=7, phantom=phantom, missing=missing,
                       withheld=withheld, notes=notes)


def test_each_heading_sits_over_the_findings_it_describes():
    """
    "We hold a row AMS does not serve" and "AMS serves a row we do not hold"
    are OPPOSITE facts with opposite fixes, and the morning log is where a
    human meets them. Swap the two headings and every count stays right, every
    finding is still printed, the whole suite still passes -- and the log
    asserts the exact reverse of the truth. The reader then goes looking for a
    stale row in the table for a lot that was never inserted, or leaves a
    phantom sitting in the index because the log filed it under the harmless
    direction. The previous build's log asserted something false in precisely
    this way, by describing both directions with one sentence.

    MUTANT THAT MUST DIE: swap the phantom and missing headings in
    report_lines(). All four are pinned, because a swap between any two of
    them is the same failure.

    Counts as well as headings: summary_line() names the same two directions
    in prose, so the findings here are deliberately 1 phantom and 2 missing --
    equal counts would make a swap there unobservable too.
    """
    f = _lopsided_findings(n_phantom=1, n_missing=2, n_withheld=1, n_notes=1)
    lines = mc.report_lines(f)
    sections = _sections(lines)
    assert len(sections) == 4, sorted(sections)

    def heading_over(marker):
        hits = [h for h, body in sections.items()
                if any(marker in line for line in body)]
        assert len(hits) == 1, f"{marker!r} appeared under {hits}"
        return hits[0]

    # Each bucket carries a slug id that appears nowhere else, so "which
    # heading is this finding under" has exactly one answer.
    assert "AMS NO LONGER SERVES THESE" in heading_over("slug 2022"), (
        "the phantom -- a row WE HOLD and AMS does not serve -- is filed "
        "under the wrong heading")
    assert "AMS SERVES THESE AND WE DO NOT HOLD THEM" in heading_over("slug 1827"), (
        "the missing row -- one AMS SERVES and we do not hold -- is filed "
        "under the wrong heading")
    assert "WITHHELD" in heading_over("slug 3333")
    assert "STILL SERVED, NO LONGER QUALIFYING" in heading_over("slug 4444")

    # The phantom heading must also still say which side holds the row, and
    # the missing heading must still refuse to be added to it.
    phantom_heading = heading_over("slug 2022")
    assert "we hold them and the index counts them" in phantom_heading
    assert "not to be added to the above" in heading_over("slug 1827")
    assert "not to be added to the above" not in phantom_heading

    # and the prose counts, in the same two directions
    assert "1 stored row(s) AMS no longer serves" in lines[0], lines[0]
    assert "2 served row(s) we do not hold" in lines[0], lines[0]
    assert "1 slug(s) withheld" in lines[0], lines[0]


def test_the_headline_pins_each_count_to_the_direction_it_describes():
    """
    THE HEADLINE ON ITS OWN, WITH NO FINDINGS UNDER IT TO CORROBORATE IT. The
    test above pins the headings by whose findings sit beneath them, which is
    a strong check and a different one. summary_line() has nothing beneath
    it -- it is three bare integers and three pieces of prose, and the ONLY
    thing tying an integer to its prose is the order of the three
    interpolations. That line prints on every run, findings or none, so on
    the ordinary morning it is the entire human-facing output of the census.

    The counts here are 1/2/3: all three unequal and all three non-zero,
    where the test above uses 1/2/1 and so leaves phantom and withheld
    interchangeable without the line changing.

    MUTANT THAT MUST DIE: swap findings.phantom and findings.missing in
    summary_line(). Also phantom <-> withheld, and missing <-> withheld: all
    three are the same failure, an integer printed against prose that means
    the opposite of it.

    Asserted on the WORDING, not on the numbers appearing somewhere in the
    line. "1 and 2 and 3 are all in there" passes on every permutation of the
    three and is exactly the shape of guard this project has been bitten by.
    """
    f = _lopsided_findings(n_phantom=1, n_missing=2, n_withheld=3)
    counts = (len(f.phantom), len(f.missing), len(f.withheld))
    assert len(set(counts)) == 3 and 0 not in counts, (
        f"the three bucket sizes are {counts}; two that are equal (or zero) "
        f"make a swap between them render an identical line, and this test "
        f"vacuous")

    line = mc.summary_line(f)

    assert "1 stored row(s) AMS no longer serves" in line, (
        f"the phantom count -- rows WE HOLD that AMS no longer serves -- is "
        f"not printed against that wording: {line!r}")
    assert "2 served row(s) we do not hold" in line, (
        f"the missing count -- rows AMS SERVES that we do not hold -- is not "
        f"printed against that wording: {line!r}")
    assert "3 slug(s) withheld" in line, (
        f"the withheld count -- slugs NOT CHECKED AT ALL -- is not printed "
        f"against that wording: {line!r}")

    # and the same line is what report_lines() leads with, on a morning with
    # findings and on the ordinary morning with none.
    assert mc.report_lines(f)[0] == line
    empty = mc.Findings(window_start="2026-09-11", window_end="2026-09-28",
                        n_compared=7)
    assert mc.report_lines(empty) == [mc.summary_line(empty)], (
        "report_lines() goes silent on a clean morning; '0 phantoms' IS the "
        "reassurance and a missing line is indistinguishable from a check "
        "that did not run")
    assert "0 stored row(s) AMS no longer serves" in mc.summary_line(empty)


def test_the_notes_bucket_stays_out_of_the_headline_counts():
    """
    notes are "served, still, but no longer qualifying" -- carried and
    written, but explicitly NOT an alarm and NOT part of the three headline
    numbers. Folding them into any of the three would inflate a count that a
    human reads as "go delete something".

    Stated as a property rather than as a fixture size: the SAME Findings
    rendered with 0 notes and with 4 must produce a byte-identical summary.
    That kills the fold-in by construction instead of by luck of the numbers.

    MUTANT THAT MUST DIE: len(findings.phantom) -> len(findings.phantom) +
    len(findings.notes), or the notes count printed as the withheld count, or
    the notes bucket substituted for any of the three.
    """
    without = _lopsided_findings(n_notes=0)
    with_notes = _lopsided_findings(n_notes=4)
    assert with_notes.notes and not without.notes, "the fixture stopped varying"
    assert mc.summary_line(with_notes) == mc.summary_line(without), (
        "four notes moved the headline counts:\n"
        f"  with notes: {mc.summary_line(with_notes)}\n"
        f"  without:    {mc.summary_line(without)}")


def test_every_finding_is_filed_under_the_heading_that_describes_it():
    """
    The detail sections again, with buckets of four DIFFERENT sizes.
    report_lines() pairs each bucket with its heading positionally in one
    tuple, so transposing two entries files every finding of both under the
    other's wording while the counts, the line order and the line count all
    stay exactly right.

    MUTANT THAT MUST DIE: swap (findings.phantom, "AMS NO LONGER SERVES...")
    with (findings.missing, "AMS SERVES THESE AND WE DO NOT HOLD THEM..."),
    and likewise withheld <-> notes.

    Checked two ways that fail independently: the slug ids under each heading,
    and the NUMBER of findings under it. The bucket sizes are unequal, so a
    swap is caught even if two buckets ever came to describe() alike -- which
    is what the 1/2/1/1 fixture above cannot promise.
    """
    f = _lopsided_findings()
    sections = _sections(mc.report_lines(f))
    assert len(sections) == 4, sorted(sections)

    def section(marker):
        hits = [h for h, body in sections.items()
                if any(marker in line for line in body)]
        assert len(hits) == 1, f"{marker!r} appeared under {hits}"
        return hits[0], sections[hits[0]]

    for slug, size, wording in (
            (PHANTOM_SLUG, len(f.phantom), "AMS NO LONGER SERVES THESE"),
            (MISSING_SLUG, len(f.missing), "AMS SERVES THESE AND WE DO NOT HOLD"),
            (WITHHELD_SLUG, len(f.withheld), "WITHHELD"),
            (NOTE_SLUG, len(f.notes), "STILL SERVED, NO LONGER QUALIFYING")):
        heading, body = section(f"slug {slug} ")
        assert wording in heading, (
            f"slug {slug}'s findings are filed under {heading!r}, which is "
            f"not the {wording!r} section")
        # describe() then detail, two lines per finding and nothing else.
        assert len(body) == 2 * size, (
            f"{wording!r} holds {len(body)} lines for {size} finding(s); a "
            f"bucket of a different size is under this heading")
        assert all(f"slug {slug} " in b for b in body[::2]), body

    # The phantom heading still says which side holds the row, and the missing
    # heading still refuses to be added to it -- the two halves of the wording
    # that make the directions readable at all.
    assert "we hold them and the index counts them" in section(
        f"slug {PHANTOM_SLUG} ")[0]
    assert "not to be added to the above" in section(
        f"slug {MISSING_SLUG} ")[0]
    assert "not to be added to the above" not in section(
        f"slug {PHANTOM_SLUG} ")[0]


# ---------------------------------------------------------------------------
# Empty is visible.
# ---------------------------------------------------------------------------

def _memory_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE mars_sales (
        report_date TEXT, raw_date TEXT, slug_id INTEGER, location TEXT,
        state TEXT, weight_low INTEGER, muscle_grade TEXT, head_count INTEGER,
        avg_weight REAL, avg_price REAL, published_date TEXT)""")
    return conn


# One barn, one sale day, three runs that differ only in WHICH direction
# fires. Same slug and same lot in all three, so "which count moved" has
# exactly one answer and it is not the fixture. HELD matches _served()'s
# defaults on every keyed field; SUPERSEDED is the McAlester shape -- the row
# a revision replaced and merge_ignore could not remove.
PANEL_SLUG = 9999
HELD = _row("2026-09-22", "2026-09-22", PANEL_SLUG, "Testville",
            750, "1", 14, 772.0, 330.29)
SUPERSEDED = _row("2026-09-22", "2026-09-22", PANEL_SLUG, "Testville",
                  750, "1", 15, 771.0, 328.32)


def _into_mars_sales(conn, *rows):
    """
    StoredRows into a real mars_sales, so run_census() reads them back through
    load_stored_groups() rather than being handed them.

    THE ONE BUILDER for this section. Five tests below put stored rows into
    a real mars_sales, and three of them carried their own copy of this
    ten-column INSERT until this round; a sixth copy arriving with the next
    test is how the column list and the schema come apart.
    """
    for r in rows:
        conn.execute(
            "INSERT INTO mars_sales (report_date, raw_date, slug_id, location, "
            "state, weight_low, muscle_grade, head_count, avg_weight, "
            "avg_price) VALUES (?,?,?,?,'OK',?,?,?,?,?)",
            (r.report_date, r.raw_date, r.slug_id, r.location, r.weight_low,
             r.muscle_grade, r.head_count, r.avg_weight, r.avg_price))
    conn.commit()


def test_a_clean_census_still_writes_the_run_row():
    """
    "0 phantoms" is the reassurance; a missing line is indistinguishable from a
    check that did not run. A findings table alone cannot tell those apart --
    both are zero rows -- so the run row is a separate POSITIVE fact.

    MUTANT THAT MUST DIE: skip the run row when there are no findings.
    """
    import mars_census_view as view

    conn = _memory_db()
    _into_mars_sales(conn, HELD)
    f = mc.run_census(conn, {PANEL_SLUG: _payload([_served()])}, *WINDOW,
                      locations={PANEL_SLUG: "Testville"})
    assert f.phantom == () and f.missing == ()

    runs = conn.execute("SELECT n_compared, n_phantom FROM mars_census_runs").fetchall()
    assert len(runs) == 1 and runs[0][0] > 0, runs
    assert conn.execute("SELECT COUNT(*) FROM mars_census").fetchone()[0] == 0

    state, summary, rows = view.census_state(conn)
    assert state == view.CLEAN
    assert rows.empty
    assert "no discrepancies" in view.headline(state, summary)
    conn.close()


def test_an_absent_census_reads_as_unavailable_and_never_as_clean():
    """
    The one substitution this panel must never make. A run that found nothing
    and a run that never happened are both zero findings, and only the run row
    tells them apart.
    """
    import mars_census_view as view

    conn = _memory_db()
    state, summary, _ = view.census_state(conn)         # no tables at all
    assert state == view.UNAVAILABLE and summary is None
    assert "unavailable" in view.headline(state, summary)
    assert "no discrepancies" not in view.headline(state, summary)

    mc.init_tables(conn)                                # tables, but no run
    state, summary, _ = view.census_state(conn)
    assert state == view.UNAVAILABLE, "an empty runs table is not a clean run"
    conn.close()


def test_findings_reach_the_table_and_the_panel():
    """The other state: a phantom must survive the round trip to the page."""
    import mars_census_view as view

    conn = _memory_db()
    stored, payloads, locs = _slug_fixture(1827, extra=[McALESTER_PHANTOM])
    _into_mars_sales(conn, *(r for rs in stored.values() for r in rs))
    mc.run_census(conn, payloads, *WINDOW, locations=locs)

    state, summary, rows = view.census_state(conn)
    assert state == view.FINDINGS
    assert int(summary["n_phantom"]) == 1
    assert list(rows["kind"]) == ["phantom"]
    line = view.headline(state, summary)
    assert "no longer publishes" in line and "no discrepancies" not in line
    conn.close()


def test_a_stale_census_says_so():
    """
    A census frozen at yesterday reads as frozen. The optional push only runs
    when update_imports.py exits 0, so an unrelated calf or corn failure
    freezes this panel while the index publishes normally.
    """
    import mars_census_view as view
    from datetime import datetime

    summary = {"run_at": "2026-09-27T07:41:00", "window_start": "2026-09-20",
               "window_end": "2026-09-28", "n_compared": 59, "n_phantom": 0,
               "n_missing": 0, "n_withheld": 0}
    fresh = view.headline(view.CLEAN, summary, datetime(2026, 9, 27, 13, 0))
    stale = view.headline(view.CLEAN, summary, datetime(2026, 9, 28, 13, 0))
    assert "hours ago" not in fresh
    assert "hours ago" in stale


# ---------------------------------------------------------------------------
# What the run leaves behind.
# ---------------------------------------------------------------------------

def test_the_findings_table_is_emptied_by_the_run_that_finds_nothing():
    """
    RUN TWICE -- findings, then clean -- because one run cannot see this at
    all. run_census() writes exactly one run row every time, so the HEADLINE
    is correct whether or not anything was truncated; the detail table below
    it is a straight insert loop. Drop db.truncate(conn, "mars_census") and
    the two halves of one panel disagree: "no discrepancies" printed over a
    list of the phantom a human removed last week. The reassuring half is the
    one that is wrong, which is the worst direction for this failure to run
    in -- a reader who trusts the headline never scrolls, and a reader who
    scrolls is told the fix did not take.

    The second run here is the ordinary morning after a fix. A phantom
    persists every run until a human removes the stored row (which is why the
    module keeps no history), so the superseded McAlester lot leaves
    mars_sales between the two runs exactly as 5083484 and 43a080c removed
    the real ones, and we then hold precisely what AMS serves.

    BOTH HALVES ARE ASSERTED, and the failure message prints them together:
    a test that only checked the table would pass on a build whose headline
    had gone wrong instead, and this panel's whole job is that its two halves
    say the same thing.

    MUTANT THAT MUST DIE: drop db.truncate(conn, "mars_census") from
    run_census().
    """
    import mars_census_view as view

    conn = _memory_db()
    stored, payloads, locs = _slug_fixture(1827, extra=[McALESTER_PHANTOM])
    _into_mars_sales(conn, *(r for rs in stored.values() for r in rs))

    # --- the morning it was found ------------------------------------------
    mc.run_census(conn, payloads, *WINDOW, locations=locs)
    state, summary, rows = view.census_state(conn)
    assert state == view.FINDINGS and list(rows["kind"]) == ["phantom"], (
        f"the premise failed: the first run has to leave one phantom on the "
        f"page for the second run to have something to clear. Got "
        f"{state!r} and {list(rows['kind'])}.")

    # --- the human removes the superseded lot, and the census runs again ----
    conn.execute(
        "DELETE FROM mars_sales WHERE slug_id = ? AND report_date = ? AND "
        "weight_low = ? AND head_count = ? AND avg_price = ?",
        (McALESTER_PHANTOM.slug_id, McALESTER_PHANTOM.report_date,
         McALESTER_PHANTOM.weight_low, McALESTER_PHANTOM.head_count,
         McALESTER_PHANTOM.avg_price))
    conn.commit()

    f = mc.run_census(conn, payloads, *WINDOW, locations=locs)
    assert f.rows == (), (
        f"the fix did not take: {[x.describe() for x in f.rows]}")

    state, summary, rows = view.census_state(conn)
    line = view.headline(state, summary)
    conn.close()

    assert state == view.CLEAN and "no discrepancies" in line, line
    assert rows.empty, (
        f"the headline reads {line!r} while the detail table under it still "
        f"lists {len(rows)} finding(s) left by the previous run: "
        f"{rows.to_dict('records')}. The two halves of one panel disagree "
        f"and the reassuring half is the wrong one.")


def test_the_panel_describes_this_run_and_not_the_one_before_it():
    """
    THREE SURVIVORS, ONE TEST, because they are one bug seen from two ends.
    census_state() picks the run it describes with
    sort_values("run_at").iloc[-1], and run_census() truncates
    mars_census_runs -- so on the shipped code there is only ever one row and
    the sort is doing nothing observable. Either half alone is defensible.
    Together they mean an edit to one silently arms the other: drop the
    truncate and the sort becomes load-bearing overnight; drop the sort, or
    read iloc[0], and the panel starts describing the older row the day a
    second one appears.

    THE SECOND ROW IS NOT HYPOTHETICAL. write_census() runs the census on a
    daemon thread behind a deadline, and an ABANDONED census never reaches
    conn.commit(), so SQLite rolls its truncate back with everything else and
    the previous run's rows survive -- measured, and pinned by
    test_an_abandoned_census_leaves_the_previous_statement_and_says_so. The
    design accepts that: the headline leads with the run_at of the run it is
    describing and mars_census_view adds "last ran N hours ago" past
    STALE_HOURS. A stale run described accurately is CORRECT. A stale run
    described as this run's all-clear is the bug, and the row-ordering half
    below is the only thing standing between the two.

    So: two run rows, the older written LAST, the older carrying a finding
    and the newer clean. Reading in insertion order, or from the front of the
    frame, reports last week's phantom this morning and does it in the
    headline, where it is not behind an expander. Then one real run_census(),
    which has to leave exactly one row rather than join the queue.

    MUTANTS THAT MUST DIE: drop db.truncate(conn, "mars_census_runs") from
    run_census(); drop the .sort_values("run_at") in census_state(); read
    .iloc[0] instead of .iloc[-1].
    """
    import mars_census_view as view

    conn = _memory_db()
    mc.init_tables(conn)

    def _run_row(run_at, n_phantom):
        conn.execute(
            "INSERT INTO mars_census_runs (run_at, window_start, window_end, "
            "n_compared, n_phantom, n_missing, n_withheld) "
            f"VALUES ({db.placeholders(7)})",
            (run_at, "2026-09-20", "2026-09-28", 7, n_phantom, 0, 0))

    # Newest first, so insertion order and chronology point opposite ways and
    # a frame read off either end gives a different answer.
    _run_row("2026-09-28T13:00:00", 0)          # this afternoon, clean
    _run_row("2026-09-21T07:30:00", 1)          # last Monday, one phantom
    conn.commit()

    state, summary, _ = view.census_state(conn)
    line = view.headline(state, summary)
    assert summary is not None, (
        f"two run rows were written and the panel reports {state!r} with no "
        f"summary at all, so there is no run for the assertions below to be "
        f"about")
    assert summary["run_at"] == "2026-09-28T13:00:00", (
        f"the panel is describing the run of {summary['run_at']}, and the "
        f"newest run in the table is 2026-09-28T13:00:00. Every count and "
        f"every date on the page belongs to the wrong morning.")
    assert state == view.CLEAN, (
        f"the newest run found nothing and the panel reads {state!r}: it is "
        f"reporting the older run's phantom as today's.")
    assert "Sep 28" in line and "Sep 21" not in line, line
    assert "no discrepancies" in line, line

    # --- and a real run replaces the pair rather than joining it -----------
    _into_mars_sales(conn, HELD)
    mc.run_census(conn, {PANEL_SLUG: _payload([_served()])}, *WINDOW,
                  locations={PANEL_SLUG: "Testville"})
    runs = conn.execute(
        "SELECT run_at, n_phantom FROM mars_census_runs").fetchall()
    conn.close()

    assert len(runs) == 1, (
        f"run_census() left {len(runs)} run rows behind: {runs}. The panel "
        f"describes exactly one of them, so every extra row is a previous "
        f"morning's statement queued up behind this one, waiting on the sort "
        f"above to keep choosing correctly -- and the table is pushed to "
        f"Snowflake nightly and never trimmed. NO HISTORY is kept, on "
        f"purpose; this is where that is true.")


# ---------------------------------------------------------------------------
# The line clients actually read.
# ---------------------------------------------------------------------------

def _phantom_only(conn):
    """We hold the superseded lot beside the live one and AMS serves only the
    live one. Nothing served is unheld and the slug was fetched, so n_phantom
    is the only count that moves."""
    _into_mars_sales(conn, HELD, SUPERSEDED)
    return mc.run_census(conn, {PANEL_SLUG: _payload([_served()])}, *WINDOW,
                         locations={PANEL_SLUG: "Testville"})


def _missing_only(conn):
    """AMS serves a qualifying lot and mars_sales holds nothing at all: we hold
    no row AMS does not serve, and the slug was fetched."""
    return mc.run_census(conn, {PANEL_SLUG: _payload([_served()])}, *WINDOW,
                         locations={PANEL_SLUG: "Testville"})


def _withheld_only(conn):
    """The slug is on the roster, we hold a row on it, and its fetch produced
    no payload. Nothing can be judged in either direction, which is the
    point."""
    _into_mars_sales(conn, HELD)
    return mc.run_census(conn, {}, *WINDOW, roster_slugs=[PANEL_SLUG],
                         locations={PANEL_SLUG: "Testville"})


def _clean_run(conn):
    """The ordinary morning: AMS serves exactly the lot we hold."""
    _into_mars_sales(conn, HELD)
    return mc.run_census(conn, {PANEL_SLUG: _payload([_served()])}, *WINDOW,
                         locations={PANEL_SLUG: "Testville"})


ONE_DIRECTION = {"n_phantom": _phantom_only,
                 "n_missing": _missing_only,
                 "n_withheld": _withheld_only}


def test_the_panel_headline_pins_each_count_to_the_direction_it_describes():
    """
    THE VIEW'S headline(), NOT mars_census.summary_line().

    The two are twins and only one of them was pinned. The test named
    test_the_headline_pins_each_count_to_the_direction_it_describes asserts on
    summary_line() -- the LOG line -- despite its name. headline() lives in
    mars_census_view.py, interpolates the same three counts into its own
    prose, and swapping its n_phantom and n_missing left the whole suite
    green. Pristine prints "1 row(s) we hold that USDA no longer publishes, 5
    published row(s) we do not hold"; the mutant printed "5 ... 1".

    THE BYTE COMPARISON DOES NOT COVER IT EITHER. mars_census_view.py is in
    test_no_drift.py's SHARED list, so the same swap made in both copies
    leaves them identical and test_every_copy_is_identical stays green as
    well. Two guards that look like they cover this line and neither does.

    The two directions demand OPPOSITE responses. A phantom is a row we hold
    that USDA no longer publishes, so the index may be OVERSTATED and a row
    should come out; a missing row is one USDA publishes and we do not hold,
    so the index may be UNDERSTATED and a row should go in. Read one as the
    other and the operator works the wrong end of the problem -- on the page
    CLIENTS READ, which is the one place this project has decided a wrong
    statement turns an internal quality line into a client question.

    MUTANT THAT MUST DIE: swap the n_phantom and n_missing interpolations in
    headline(). Also phantom <-> withheld and missing <-> withheld: all three
    are the same failure, an integer printed against prose that means the
    opposite of it.

    1/2/3, taken from the same _lopsided_findings() the summary_line test
    uses rather than from a second fixture that happens to agree. All three
    unequal and all three non-zero, because equal counts make a swap render a
    byte-identical line and this assertion vacuous.

    Asserted on the WORDING, not on the numbers being present somewhere in
    the line. "1 and 2 and 3 are all in there" passes on every permutation of
    the three and is exactly the shape of guard this project has been bitten
    by.
    """
    import mars_census_view as view

    f = _lopsided_findings(n_phantom=1, n_missing=2, n_withheld=3)
    summary = {"run_at": "2026-09-28T07:41:00",
               "window_start": f.window_start, "window_end": f.window_end,
               "n_compared": f.n_compared, "n_phantom": len(f.phantom),
               "n_missing": len(f.missing), "n_withheld": len(f.withheld)}
    counts = (summary["n_phantom"], summary["n_missing"], summary["n_withheld"])
    assert len(set(counts)) == 3 and 0 not in counts, (
        f"the three counts are {counts}; two that are equal (or zero) make a "
        f"swap between them render an identical line, and this test vacuous")

    line = view.headline(view.FINDINGS, summary)

    assert "1 row(s) we hold that USDA no longer publishes" in line, (
        f"the phantom count -- rows WE HOLD that USDA no longer publishes, "
        f"where the index may be OVERSTATED -- is not printed against that "
        f"wording: {line!r}")
    assert "2 published row(s) we do not hold" in line, (
        f"the missing count -- rows USDA PUBLISHES that we do not hold, where "
        f"the index may be UNDERSTATED -- is not printed against that "
        f"wording: {line!r}")
    assert "3 report(s) that could not be checked" in line, (
        f"the withheld count -- reports NOT CHECKED AT ALL -- is not printed "
        f"against that wording: {line!r}")

    # and the clean morning still says the reassuring thing, with none of the
    # three counts on it: one function in two shapes, and pinning only the
    # loud shape leaves the quiet one free to start printing numbers.
    clean = view.headline(view.CLEAN, summary)
    assert "no discrepancies" in clean, clean
    assert "row(s) we hold" not in clean and "we do not hold" not in clean, clean


@pytest.mark.parametrize("build,label", [
    (_clean_run, "a run that found nothing"),
    (_phantom_only, "a run that found a phantom"),
])
def test_a_findings_table_that_will_not_read_is_unavailable_not_clean(build, label):
    """
    BOTH READS MUST SUCCEED -- census_state()'s own docstring, and the one
    thing it says it must not do.

    The runs row opening while the findings table does not is a REAL and
    asymmetric failure rather than a contrived one: the two tables are pushed
    separately and are separately optional in 02_migrate_data.py, so one can
    perfectly well be there and the other not. It is also the failure that
    reads hardest, because the runs row carries the counts and a caller that
    trusts them renders a confident line over a findings table nobody could
    open.

    test_an_absent_census_reads_as_unavailable_and_never_as_clean covers
    NEITHER table being there, which the FIRST read fails on. This covers only
    the second one failing -- and census_state() could be made to survive that
    and return clean or findings with the whole suite green.

    Both substitutions are wrong and both are asserted:
      * the clean run must not render "no discrepancies", which would be a
        reassurance printed over a table nobody read;
      * the run with findings must not render its counts either, because the
        detail rows that back them could not be read and the panel would be
        quoting half a census.

    MUTANT THAT MUST DIE: move the mars_census read out of the try, or give it
    its own except that falls back to an empty frame, so an unreadable
    findings table reads as no findings.
    """
    import mars_census_view as view

    conn = _memory_db()
    build(conn)

    # The precondition, asserted rather than assumed: the runs row is present
    # AND populated, so what follows is about the second read alone.
    run = conn.execute("SELECT n_compared, n_phantom, n_missing, n_withheld "
                       "FROM mars_census_runs").fetchall()
    assert len(run) == 1 and run[0][0] > 0, run

    conn.execute("DROP TABLE mars_census")
    conn.commit()
    # And the read really does fail. A test whose "failing read" quietly
    # succeeds is the compound tautology this project keeps writing.
    with pytest.raises(Exception):
        db.read_sql_lower("SELECT kind FROM mars_census", conn)

    state, summary, rows = view.census_state(conn)
    assert state == view.UNAVAILABLE, (
        f"{label}, with an unreadable findings table, rendered as {state!r}; "
        f"the runs row is readable and the findings are not, which is not a "
        f"census result of either kind")
    assert summary is None, (
        f"summary is {summary!r} on an unavailable census; a caller can now "
        f"read a count off a run whose findings nobody could open")
    assert rows.empty

    line = view.headline(state, summary)
    assert "unavailable" in line, line
    assert "no discrepancies" not in line, line
    assert "row(s) we hold" not in line, line
    conn.close()


@pytest.mark.parametrize("which", ["n_phantom", "n_missing", "n_withheld"])
def test_a_run_with_only_one_kind_of_finding_still_reads_as_findings(which):
    """
    census_state() decides clean-or-findings by summing THREE counts, and
    dropping any one of them from that sum renders a run whose only findings
    are of that kind as "AMS reconciliation ... no discrepancies" -- on the
    page clients read, with the findings sitting in the table underneath it.

    test_findings_reach_the_table_and_the_panel exercises the phantom
    direction only, so n_missing and n_withheld could each be dropped from the
    sum with nothing failing. A missing-only morning is not exotic: it is what
    a barn we quietly stopped ingesting looks like, and it is the direction
    that says the index is UNDERSTATED.

    MUTANT THAT MUST DIE: drop "n_missing" from the tuple census_state() sums.
    Also "n_phantom" and "n_withheld" -- parametrised because all three are
    the same failure and covering one covers one.

    Each case is a REAL run_census() against a real mars_sales rather than a
    run row written by hand, so the count being non-zero is the pipeline's own
    doing. The other two counts are asserted to be ZERO: a case that lit up
    two directions would stay green on the mutant that drops either.
    """
    import mars_census_view as view

    conn = _memory_db()
    findings = ONE_DIRECTION[which](conn)

    got = dict(zip(("n_phantom", "n_missing", "n_withheld"),
                   (len(findings.phantom), len(findings.missing),
                    len(findings.withheld))))
    assert got[which] > 0, (
        f"the {which} fixture produced {got}; a case whose own direction is "
        f"empty cannot show that census_state() reads it")
    assert all(v == 0 for k, v in got.items() if k != which), (
        f"the {which} fixture produced {got}; a second non-zero count would "
        f"keep this green on the mutant that drops {which}")

    state, summary, rows = view.census_state(conn)
    assert state == view.FINDINGS, (
        f"a run whose only findings are {which} rendered as {state!r}; the "
        f"page then tells the client there are no discrepancies while "
        f"mars_census underneath it holds {got[which]}")
    assert int(summary[which]) == got[which]
    assert "no discrepancies" not in view.headline(state, summary)
    conn.close()


# ---------------------------------------------------------------------------
# The push.
# ---------------------------------------------------------------------------

def _migrate():
    """snowflake/02_migrate_data.py, loaded without running its __main__."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_migrate", REPO / "snowflake" / "02_migrate_data.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_census_tables_are_optional_and_pushed_first():
    """
    NEVER CRITICAL: a census push failure must not be reported as an index
    failure. FIRST, so a few rows land before the 73k-row tables and an
    unrelated failure later in the list cannot freeze the panel at yesterday.
    """
    m = _migrate()
    assert m.OPTIONAL_TABLES[:2] == ["mars_census", "mars_census_runs"]
    assert not (set(m.CRITICAL_TABLES) & CENSUS_TABLES_EXPECTED)


CENSUS_TABLES_EXPECTED = {"mars_census", "mars_census_runs"}


def test_a_missing_census_table_cannot_strand_the_other_dashboard_tables():
    """
    THE TRAP, from both ends. main() reads the local table and runs DESC TABLE
    outside its transaction's try, so either one raising takes down the whole
    remaining push. mars_census is created by a census call that is DESIGNED to
    be allowed to fail -- so without the preflight, a failed diagnostic would
    make every dashboard table after it stale.

    MUTANT THAT MUST DIE: drop the `if table in OPTIONAL_TABLES` preflight.
    """
    m = _migrate()

    class _Cur:
        def execute(self, sql):
            raise RuntimeError("Table 'MARS_CENSUS' does not exist")

    class _SF:
        def cursor(self):
            return _Cur()

    conn = _memory_db()
    assert "absent from SQLite" in m.unreadable(conn, _SF(), "mars_census")

    mc.init_tables(conn)
    assert "absent from Snowflake" in m.unreadable(conn, _SF(), "mars_census")

    class _OK(_SF):
        def cursor(self):
            return type("C", (), {"execute": lambda s, q: [("KIND",)]})()

    assert m.unreadable(conn, _OK(), "mars_census") is None
    conn.close()


def test_the_push_actually_calls_the_preflight_and_calls_it_first():
    """
    THE HELPER IS NOT THE GUARD; THE CALL IS. A mutation run caught this: with
    unreadable() perfect but never invoked, the test above stayed green and the
    trap was wide open. Read off the AST so a mention in a comment cannot
    satisfy it, and check the ORDER too -- a preflight after the read it exists
    to protect is decoration.
    """
    src = (REPO / "snowflake" / "02_migrate_data.py").read_text(encoding="utf-8")
    loop = None
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.For) and ast.unparse(node.target) == "table":
            loop = node
            break
    assert loop is not None, "02_migrate_data.py no longer has a per-table loop"

    calls = {ast.unparse(n.func): n.lineno for n in ast.walk(loop)
             if isinstance(n, ast.Call)}
    assert "unreadable" in calls, (
        "the push loop no longer preflights optional tables; a table absent "
        "from SQLite or from Snowflake would raise out of the loop and strand "
        "every table after it.")
    reads = [n.lineno for n in ast.walk(loop) if isinstance(n, ast.Call)
             and ast.unparse(n.func) == "pd.read_sql"]
    assert reads and calls["unreadable"] < min(reads), (
        "the preflight runs after the read it protects.")

    # And it must be scoped to OPTIONAL tables: a critical table that cannot be
    # read is a real failure and must still stop the push.
    guarded = [ast.unparse(n.test) for n in ast.walk(loop)
               if isinstance(n, ast.If) and "unreadable" in ast.unparse(n)]
    assert any("OPTIONAL_TABLES" in t for t in guarded), guarded


# ---------------------------------------------------------------------------
# The numbers that must not move.
# ---------------------------------------------------------------------------

GROUND_TRUTH = {"2026-09-22": 336.9776, "2026-09-23": 337.0698,
                "2026-09-24": 338.7890, "2026-09-25": 337.7945,
                "2026-09-28": 337.7713}


def test_the_published_index_did_not_move():
    """
    Lifting the date derivation out of run_update()'s roster loop touches the
    most load-bearing code in the repository. It is a pure move of six lines,
    but "pure move" is what everyone says -- so recompute the whole index from
    the stored rows and check it against the values CME printed against.

    FIVE DATES, NOT FOUR. 2026-09-22..09-28 is one whole CME index week --
    Tue, Wed, Thu, Fri, Mon, because Saturday and Sunday fold into Monday and
    a CME week therefore has five index days. Every one of the five is ours to
    the cent against cme_ftp_daily: 336.98, 337.07, 338.79, 337.79, 337.77.
    09-28 is also the date that settled the preliminary-OKC question -- CME
    printed 337.7700 against our 337.7713 while CIH called 337.78.

    WHAT THE FIFTH PIN BUYS, MEASURED RATHER THAN ASSUMED: no mutation of the
    rolling-window arithmetic moves 09-28's VALUE while leaving the other four
    standing, because that arithmetic is shared by every date. It is a
    regression pin on a real published number, not a mutation kill, and it is
    described that way on purpose. There is no weekend row anywhere in
    09-22..09-28 either, so 09-28's bucket is assembled exactly like the other
    four and this pin does NOT exercise the Saturday fold; test_ericson_
    saturdays_are_keyed_on_the_monday_they_are_stored_under is what does.

    THE BOUNDS ARE READ OFF GROUND_TRUTH rather than written out again. The
    dict and a hardcoded WHERE clause drifting apart IS the finding this
    replaced -- four keys against a 09-22..09-25 window, with keeping them in
    step nobody's job.

    THE RANGE ASSERTION IS THE ONE WITH TEETH AGAINST TRUNCATION, and it is
    structural on purpose. An earlier draft of this test claimed to kill
    `all_dates[0], all_dates[-3]` by pinning a value near the tail. That kill
    was real for about four hours and then the ordinary 13:00 ingest added a
    bucket date and it stopped being real -- three reviewers reproduced the
    mutant PASSING. A kill whose truth depends on how many trailing sale days
    the table happens to hold today is not a kill; it is a docstring that goes
    false on its own. So the range is derived from the same rows on both
    sides instead: recompute_fci_daily() must cover exactly the span of bucket
    dates mars_sales yields, and that statement is as true at 33,296 rows as
    at 33,225.

    MUTANTS THAT MUST DIE: `all_dates[-1]` -> `all_dates[-2]`, `[-3]` or
    `[-4]`; `all_dates[0]` -> `all_dates[1]`; `while d <= last_date` ->
    `while d < last_date`. Each stops the series short of a date the table has
    rows for, and none of them moves a pinned value.

    WHAT THE RANGE ASSERTION DOES NOT COVER, stated rather than implied: both
    sides run through ui.shifted_bucket_date(), so a mutation of the BUCKETING
    RULE moves them together and this stays green. The five pinned values are
    what hold the bucketing, and tests/test_weekend_line.py holds the fold.

    Read-only on the live database, recomputed in memory: data/mars_history.db
    is pushed to production and read by the dashboard, and runs fire at 07:45
    and 13:00.
    """
    live = REPO / "data" / "mars_history.db"
    if not live.exists():
        pytest.skip("no local database")
    src = sqlite3.connect(f"file:{live.as_posix()}?mode=ro", uri=True)
    try:
        rows = src.execute(
            "SELECT report_date, raw_date, slug_id, location, state, "
            "weight_low, muscle_grade, head_count, avg_weight, avg_price, "
            "published_date FROM mars_sales").fetchall()
    finally:
        src.close()

    conn = _memory_db()
    conn.execute("""CREATE TABLE fci_daily (
        report_date TEXT PRIMARY KEY, fci_value REAL NOT NULL,
        n_locations INTEGER NOT NULL, total_head INTEGER NOT NULL,
        same_day_price REAL, same_day_head INTEGER, same_day_avg_weight REAL)""")
    conn.executemany(
        "INSERT INTO mars_sales VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    ui.recompute_fci_daily(conn)
    written = dict(conn.execute(
        "SELECT report_date, fci_value FROM fci_daily").fetchall())
    conn.close()
    assert written, "recompute_fci_daily() wrote no index at all"

    # THE SPAN, derived from the same rows the recompute was handed. Not a
    # pinned date: a pinned date would have to be rechosen every sale day.
    buckets = {ui.shifted_bucket_date(location, report_date)
               for report_date, _, _, location, *_ in rows}
    assert (min(written), max(written)) == (min(buckets), max(buckets)), (
        f"fci_daily covers {min(written)}..{max(written)} while mars_sales "
        f"has rows bucketed across {min(buckets)}..{max(buckets)}. The index "
        f"for the dates outside that span is not wrong, it is ABSENT -- the "
        f"recompute stopped short, so the dashboard and the push both see a "
        f"series that ends early, and the newest dates are the ones JSA "
        f"publishes today and CME has not printed yet.")

    got = {d: v for d, v in written.items()
           if min(GROUND_TRUTH) <= d <= max(GROUND_TRUTH)}
    missing = sorted(d for d in GROUND_TRUTH if d not in got)
    assert not missing, (
        f"recompute_fci_daily() wrote no row at all for {', '.join(missing)}. "
        f"The index for those dates is not wrong, it is ABSENT.")

    for d, want in sorted(GROUND_TRUTH.items()):
        assert round(got[d], 4) == want, f"{d}: {got[d]:.4f} != {want}"


def test_mars_sales_really_holds_lots_that_only_muscle_grade_separates():
    """
    The premise of the two muscle_grade tests above, checked against the
    product's own data rather than argued: mars_sales holds rows that agree on
    every key field EXCEPT muscle_grade. Four such groups on 2026-09-30 --
    1814 on 2024-03-19, 1954 on 2025-01-23, 1782 on 2025-03-17, 1828 on
    2025-10-09 -- each a #1 and a #1-2 lot at one price, one weight break and
    one head count.

    Dropping muscle_grade from key_of() would merge every one of them, so the
    census would treat eight stored rows as four and could never report a
    withdrawal of either half. The collision is not hypothetical and this is
    what says so.

    Read-only on the live database, following the test above:
    data/mars_history.db is pushed to production and runs fire at 07:45 and
    13:00. The count is asserted as ">= 1" rather than "== 4" because
    mars_sales only grows -- pinning the number would turn an ordinary ingest
    into a failing test.
    """
    live = REPO / "data" / "mars_history.db"
    if not live.exists():
        pytest.skip("no local database")
    src = sqlite3.connect(f"file:{live.as_posix()}?mode=ro", uri=True)
    try:
        rows = src.execute(
            "SELECT report_date, slug_id, weight_low, avg_price, head_count, "
            "muscle_grade FROM mars_sales WHERE (report_date, slug_id, "
            "weight_low, avg_price, head_count) IN (SELECT report_date, "
            "slug_id, weight_low, avg_price, head_count FROM mars_sales "
            "GROUP BY 1,2,3,4,5 HAVING COUNT(DISTINCT muscle_grade) > 1) "
            "ORDER BY 1,2,3,4,5,6").fetchall()
    finally:
        src.close()

    groups = {}
    for report_date, slug_id, weight_low, avg_price, head_count, grade in rows:
        groups.setdefault(
            (report_date, slug_id, weight_low, avg_price, head_count),
            []).append(grade)
    assert groups, (
        "mars_sales no longer holds a pair of lots separated only by "
        "muscle_grade, so this test cannot see the field leave the key. It "
        "held four such groups on 2026-09-30 and the table only grows -- if "
        "this fires, something removed rows and the premise needs rechecking.")

    for g, grades in sorted(groups.items()):
        report_date, slug_id, weight_low, avg_price, head_count = g
        keys = {mc.key_of(report_date, slug_id, weight_low, grade, avg_price,
                          head_count) for grade in grades}
        assert len(keys) == len(grades), (
            f"slug {slug_id} {report_date}: {len(grades)} real lots "
            f"({', '.join(grades)}) at {weight_low} lb, {head_count} head, "
            f"${avg_price} collapse to {len(keys)} key(s). muscle_grade is "
            f"not in the key, so one of them masks the other.")


@lru_cache(maxsize=1)
def _live_prices():
    """
    Every DISTINCT avg_price in mars_sales, or None when there is no local
    database.

    Read-only on the live database, following the two tests above:
    data/mars_history.db is pushed to production and runs fire at 07:45 and
    13:00. DISTINCT rather than every row because the same price at two barns
    is one key by design; what the tests below are about is whether two
    DIFFERENT prices can become one.
    """
    live = REPO / "data" / "mars_history.db"
    if not live.exists():
        return None
    src = sqlite3.connect(f"file:{live.as_posix()}?mode=ro", uri=True)
    try:
        return tuple(p for (p,) in src.execute(
            "SELECT DISTINCT avg_price FROM mars_sales "
            "WHERE avg_price IS NOT NULL"))
    finally:
        src.close()


def _prices_sharing_a_key(cents, prices):
    """
    [(key, [cent value, ...])] for every key `cents` hands to two or more
    prices that are NOT the same price to the cent.

    THE CENT IS ESTABLISHED INDEPENDENTLY OF THE FUNCTION UNDER TEST.
    Decimal(str(p)).quantize() is exact decimal arithmetic on the shortest
    repr and shares no step with round(float(p) * 100); a helper that decided
    "same cent" by calling price_cents() would be the tautology this file's
    header warns about, and this repository has shipped three of those.

    A key holding two prices that ARE the same cent is not a collision but the
    whole point -- see test_a_price_that_drifted_in_its_last_bit_is_still_one_
    key -- so those are not reported.
    """
    buckets = {}
    for p in prices:
        buckets.setdefault(cents(p), set()).add(
            Decimal(str(p)).quantize(Decimal("0.01")))
    return [(k, sorted(v)) for k, v in sorted(buckets.items()) if len(v) > 1]


def test_price_cents_never_merges_two_live_prices_that_differ_in_cents():
    """
    THE PROPERTY, OVER THE PRICES THE PRODUCT ACTUALLY HOLDS rather than over
    a pair someone typed -- which is what the key test relied on, and the pair
    it had chosen sat off the failure. Every distinct avg_price in mars_sales,
    bucketed by the key price_cents() gives it: no bucket may hold two
    different cent values. 12,046 distinct prices on 2026-09-30 and zero such
    buckets.

    A key that merged two of them would merge two lots -- AMS's correction and
    the superseded copy it replaced -- so the phantom and the missing row
    cancel and the census reports nothing at all. That end-to-end consequence
    is test_a_one_cent_price_correction_shows_up_in_both_directions().

    Read-only on the live database, following the tests above:
    data/mars_history.db is pushed to production and runs fire at 07:45 and
    13:00.

    MUTANT THAT MUST DIE: return int(float(Decimal(str(p))) * 100) from
    price_cents().
    """
    prices = _live_prices()
    if prices is None:
        pytest.skip("no local database")
    assert len(prices) > 1000, (
        f"only {len(prices)} distinct price(s) -- too few to be the live "
        f"table, and a sample that small could pass this vacuously")

    # THE PREMISE OF "the same price to the cent", asserted rather than
    # assumed. 0 of the 33,286 stored rows carried more than two decimal
    # places on 2026-09-30 and AMS quotes cwt to the cent, so every price
    # here is exactly one cent value. If that ever stops being true this
    # fires FIRST and says why, instead of the collision assert below firing
    # on a pair that is genuinely half a cent apart and reading as a bug in
    # price_cents().
    finer = [p for p in prices if Decimal(str(p)).as_tuple().exponent < -2]
    assert not finer, (
        f"AMS is serving prices finer than a cent, so 'the same price to the "
        f"cent' is no longer the right equivalence for this check and it "
        f"needs rewriting rather than silencing: {sorted(finer)[:5]}")

    merged = _prices_sharing_a_key(mc.price_cents, prices)
    assert merged == [], (
        f"{len(merged)} price_cents() key(s) are shared by prices a cent or "
        f"more apart, so a one-cent revision at those prices is invisible to "
        f"the census: {merged[:5]}")


def test_a_truncating_price_key_really_would_merge_live_prices():
    """
    THE PAIRED KNOWN-VIOLATION TEST for the one above, which would otherwise
    be a guard nobody has seen fail. Feed the same helper the implementation
    price_cents() must NOT be -- int() where it has int(round()) -- and the
    live prices must produce collisions. If they stop doing so, the test above
    has quietly become unfalsifiable and the pair every price test in this
    file names has to be rechosen.

    INDEPENDENTLY DERIVED 2026-09-30, read-only: 415 of the 12,046 distinct
    avg_price values in mars_sales share a truncated key with another price,
    every one of those keys holding exactly two, and 1,074 of the 33,286
    stored rows land on a different integer under truncation than under
    rounding. 256.02 and 256.03 are one such pair and both are stored.

    Asserted as ">= 1" rather than "== 415" because mars_sales only grows and
    pinning the count would turn an ordinary ingest into a failing test. The
    named pair is asserted exactly, because it is the one the other price
    tests are built on and it is the thing that must not quietly stop being
    true.
    """
    prices = _live_prices()
    if prices is None:
        pytest.skip("no local database")

    def truncating(p):
        return int(float(Decimal(str(p))) * 100)

    merged = _prices_sharing_a_key(truncating, prices)
    assert merged, (
        "truncation no longer merges any two stored prices, so "
        "test_price_cents_never_merges_two_live_prices_that_differ_in_cents "
        "is a check that cannot fail")
    assert [cents for key, cents in merged if key == 25602] == [
        [Decimal("256.02"), Decimal("256.03")]], (
        "mars_sales no longer holds both 256.02 and 256.03, the pair the "
        "price tests above are built on: pick another from this list and "
        f"update them together -- {merged[:5]}")
