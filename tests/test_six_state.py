"""
The 6 State Weekly Feeder Cattle Prices table.

Pinned against ROSS'S OWN SHEET for the week of 8/30-9/5/2026, because the
arithmetic in `six_state.py` was reverse-engineered from it rather than
specified: the whole value of the tab is that it reproduces a table he was
building by hand, and every rule in it has a plausible alternative that
renders perfectly and quietly prints a different number. Including grade 1-2
moves Missouri 6-700 by $15.71; pound-weighting instead of head-weighting
moves Oklahoma 7-899 by $0.52; subtracting the unrounded averages moves
Oklahoma 5-600's MoM by a cent. None of those raises anything.

The fixture is the real AMS payload for the two weeks the table needs --
every Steers row between 500 and 899 lb, filtered no further, so the
grade 1-2 / per-head / Replacement Cattle rows the page must EXCLUDE are
present and the filter has something to bite on.

FIVE OF THE EIGHTEEN CELLS DO NOT MATCH HIS SHEET AND ARE PINNED AS THEY ARE,
not as he printed them. All five are Arkansas (3) or a 7-899 weight (2), and
AMS revised those rows after he took the screenshot -- a brute-force search
over every subset of the Arkansas rows found a UNIQUE subset reproducing each
of his two figures, and in both cases it drops single-head lots that are in
the feed today. Adopting that as a rule breaks Oklahoma 5-600, so it is not a
rule, it is a restatement. Two of the three Arkansas MoM BASES still match his
to the cent, which is the strongest single piece of evidence for the
base-week rule in this file.
"""
import datetime as dt
import json
import os
import sys
from pathlib import Path

import pytest

# This file is shared verbatim with the cme-feeder-cattle-index repo, where the
# module sits at the repo root rather than under apps/cme_feeder_cattle -- so
# both layouts are tried rather than hardcoding the portal's. APP_DIR is then
# whichever one answered, and the page-level tests below read THAT repo's
# app.py, so each copy's own wiring is the thing being checked. Listed in
# SHARED in cme-feeder-cattle-index/tests/test_no_drift.py.
_HERE = os.path.dirname(os.path.abspath(__file__))
for _rel in (("..", "apps", "cme_feeder_cattle"), ("..",)):
    _cand = os.path.join(_HERE, *_rel)
    if os.path.isfile(os.path.join(_cand, "six_state.py")):
        sys.path.insert(0, _cand)
        break
APP_DIR = Path(_cand).resolve()

import six_state as ss  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "six_state_ams.json"
WEEK = dt.date(2026, 8, 30)
BASE = dt.date(2026, 7, 26)


@pytest.fixture(scope="module")
def data():
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return {"states": {c: [ss._trim(r) for r in rows] for c, rows in raw.items()},
            "errors": {}}


@pytest.fixture(scope="module")
def rendered(data):
    ws = ss.weeks(data)
    week = next(w for w in ws if w["begin"] == WEEK)
    base = ss.week_containing(ws, ss.month_before(WEEK))
    return {(r["state"], r["bracket"]): (ss.price_range(r["now"]),
                                         ss.wtd_average(r["now"]),
                                         ss.mom_change(r["mom"]))
            for r in ss.table(data, week, base)}


# What the page prints today, cell for cell. Entries marked `# revised` are
# the five that differ from Ross's screenshot; his figure is in the comment so
# a future reader can see the size of the gap rather than take it on trust.
EXPECTED = {
    ("Arkansas", "5-600#"): ("328 - 423", "550# @ $374.72", "-30.99/cwt"),      # his 551# @ $373.77, -31.94
    ("Arkansas", "6-700#"): ("310 - 382.50", "637# @ $344.05", "-21.58/cwt"),   # his $344.33, -21.30
    ("Arkansas", "7-899#"): ("270 - 363", "757# @ $326.76", "-17.03/cwt"),      # his -4.52
    ("Iowa", "5-600#"): ("379 - 500", "557# @ $406.59", "-20.82/cwt"),
    ("Iowa", "6-700#"): ("348 - 410", "639# @ $383.54", "-22.77/cwt"),
    ("Iowa", "7-899#"): ("291 - 375", "828# @ $341.07", "-18.25/cwt"),          # his 826#, -21.76
    ("Kansas", "5-600#"): ("375 - 434", "549# @ $404.79", "-18.41/cwt"),
    ("Kansas", "6-700#"): ("338 - 400", "635# @ $363.55", "-29.44/cwt"),
    ("Kansas", "7-899#"): ("304.50 - 366", "812# @ $332.48", "-19.72/cwt"),
    ("Missouri", "5-600#"): ("359 - 427.50", "558# @ $391.39", "-54.58/cwt"),
    ("Missouri", "6-700#"): ("340 - 401", "641# @ $373.11", "-18.22/cwt"),
    ("Missouri", "7-899#"): ("300 - 379", "794# @ $336.03", "-9.45/cwt"),
    ("Nebraska", "5-600#"): ("N/A", "N/A", "N/A"),
    ("Nebraska", "6-700#"): ("375 - 390", "645# @ $383.65", "N/A"),
    ("Nebraska", "7-899#"): ("329 - 362.50", "834# @ $350.31", "+1.49/cwt"),
    ("Oklahoma", "5-600#"): ("300 - 424.50", "538# @ $387.24", "-39.07/cwt"),
    ("Oklahoma", "6-700#"): ("308.50 - 398", "642# @ $363.08", "-4.88/cwt"),
    ("Oklahoma", "7-899#"): ("296 - 367", "822# @ $324.31", "-18.36/cwt"),
}


@pytest.mark.parametrize("key", sorted(EXPECTED))
def test_every_cell_matches_the_sheet(rendered, key):
    assert rendered[key] == EXPECTED[key]


def test_the_table_is_eighteen_cells_in_page_order(data):
    ws = ss.weeks(data)
    rows = ss.table(data, next(w for w in ws if w["begin"] == WEEK), None)
    assert len(rows) == 18
    assert [r["state"] for r in rows[:3]] == ["Arkansas"] * 3
    assert [r["bracket"] for r in rows[:3]] == ["5-600#", "6-700#", "7-899#"]
    # The page lays the first nine down the left and the last nine down the
    # right, so this order IS the layout.
    assert [r["state"] for r in rows[9:12]] == ["Missouri"] * 3


# ── the filters, each of which fails silently ────────────────────────────────

def test_grade_1_2_is_excluded_and_it_matters(data):
    """
    "Medium and Large 1" means muscle_grade "1". Admitting "1-2" as well is the
    single most tempting loosening -- the rows sit beside each other and look
    like the same cattle -- and on Missouri 6-700 it moves the printed average
    from $373.11 to $357.40.
    """
    rows = data["states"]["MO"]
    strict = ss.cell(rows, WEEK, 600, 700)
    loose = [r for r in rows
             if r["begin"] == WEEK and r["commodity"] == "Feeder Cattle"
             and r["class"] == "Steers" and r["frame"] == "Medium and Large"
             and r["grade"] in ("1", "1-2") and r["unit"] == "Per Cwt"
             and 600 <= (r["wlow"] or 0) < 700 and r["head"] and r["weight"]
             and r["price"]]
    head = sum(r["head"] for r in loose)
    widened = round(sum(r["head"] * r["price"] for r in loose) / head, 2)
    assert strict["price"] == 373.11
    assert widened == 357.40
    assert any(r["grade"] == "1-2" for r in loose), "fixture lost its 1-2 rows"


def test_per_head_lots_are_dropped_not_converted(data):
    """
    AMS quotes small lots PER ANIMAL in the same `avg_price` column. Reading
    one as $/cwt is the trap `calf_sales.py` documents, where a per-head price
    made one barn the chart leader at $1,118.70/cwt.

    Synthetic, deliberately: these two weeks happen to carry no per-head Steers
    lot between 500 and 899 lb, so a fixture-only test would pass on an empty
    set and prove nothing. The row below is a real AMS shape -- four head of
    520 lb calves at $2,010.00 Per Unit, which is $386.54/cwt.
    """
    rows = list(data["states"]["KS"])
    assert ss.cell(rows, WEEK, 500, 600)["price"] == 404.79

    per_head = dict(rows[0], begin=WEEK, commodity=ss.COMMODITY, **{
        "class": ss.CLASS, "frame": ss.FRAME, "grade": ss.GRADE,
        "unit": "Per Unit", "wlow": 500.0, "head": 4.0, "weight": 520.0,
        "price": 2010.0, "price_low": 2010.0, "price_high": 2010.0})
    rows.append(per_head)

    assert per_head not in ss._qualifying(rows, WEEK, 500, 600)
    assert ss.cell(rows, WEEK, 500, 600)["price"] == 404.79, \
        "a per-head lot reached the average"
    # ...and it would have been ruinous rather than negligible: unfiltered,
    # four head drags the whole bracket up by thirty-four dollars.
    kept = ss._qualifying(rows, WEEK, 500, 600) + [per_head]
    head = sum(r["head"] for r in kept)
    assert round(sum(r["head"] * r["price"] for r in kept) / head, 2) == 439.13


def test_only_feeder_cattle_counts(data):
    """
    The same class, frame and grade also appear under Replacement Cattle and
    Slaughter Cattle. Those are different animals on a different trade.
    """
    for code, rows in data["states"].items():
        for low, high in ((500, 600), (600, 700), (700, 900)):
            kept = ss._qualifying(rows, WEEK, low, high)
            assert all(r["commodity"] == ss.COMMODITY for r in kept), code


def test_special_conditions_are_included_not_filtered(data):
    """
    "Includes Special Conditions" in the title is a statement, not a filter --
    Value Added, Fleshy, Thin Fleshed, Unweaned and the rest all count. On
    Arkansas 5-600 the Value Added lots are 12 of 118 head and dropping every
    descriptor would change the figure.
    """
    kept = ss._qualifying(data["states"]["AR"], WEEK, 500, 600)
    assert {r["lot_desc"] for r in kept} - {None, "None"}, \
        "no descriptor rows survived -- the filter has gone too far"


# ── the two weightings, and the rounding ─────────────────────────────────────

def test_averages_are_head_weighted_not_pound_weighted(data):
    """
    The rest of this dashboard is pound-weighted (`cash_calves.py` says so in
    its first paragraph). This table is not, because the source sheet is not:
    pound-weighting Oklahoma 7-899 gives $323.79 against the $324.31 Ross
    printed and the page shows.
    """
    rows = ss._qualifying(data["states"]["OK"], WEEK, 700, 900)
    pounds = sum(r["head"] * r["weight"] for r in rows)
    by_pound = round(sum(r["head"] * r["weight"] * r["price"] for r in rows) / pounds, 2)
    assert ss.cell(data["states"]["OK"], WEEK, 700, 900)["price"] == 324.31
    assert by_pound == 323.79


def test_mom_subtracts_the_printed_figures_not_the_raw_ones(data):
    """
    The FCI convention, for the reason CLAUDE.md gives: a reader holding two
    weeks of this table must be able to subtract the two printed averages and
    get the printed change. Oklahoma 5-600 is the cell that separates them --
    -39.07 printed against -39.0766 raw, which rounds the other way.
    """
    rows = data["states"]["OK"]
    now, base = ss.cell(rows, WEEK, 500, 600), ss.cell(rows, BASE, 500, 600)
    assert (now["price"], base["price"]) == (387.24, 426.31)
    assert round(now["price"] - base["price"], 2) == -39.07

    raw_now = _unrounded(ss._qualifying(rows, WEEK, 500, 600))
    raw_base = _unrounded(ss._qualifying(rows, BASE, 500, 600))
    assert round(raw_now - raw_base, 2) == -39.08, \
        "the raw difference must still round the other way, or this proves nothing"


def _unrounded(rows):
    head = sum(r["head"] for r in rows)
    return sum(r["head"] * r["price"] for r in rows) / head


# ── the base week ────────────────────────────────────────────────────────────

def test_the_mom_base_is_a_calendar_month_back(data):
    """
    For the week beginning 8/30 that is 7/30, whose week begins 7/26. Four
    weeks back is 8/2 and reproduces none of Ross's changes; five weeks back
    happens to agree here and would not in a month with a short February.
    """
    ws = ss.weeks(data)
    assert ss.month_before(WEEK) == dt.date(2026, 7, 30)
    assert ss.week_containing(ws, ss.month_before(WEEK))["begin"] == BASE


def test_month_before_clamps_into_a_short_month():
    assert ss.month_before(dt.date(2026, 3, 31)) == dt.date(2026, 2, 28)
    assert ss.month_before(dt.date(2024, 3, 30)) == dt.date(2024, 2, 29)
    assert ss.month_before(dt.date(2026, 1, 15)) == dt.date(2025, 12, 15)


def test_a_bracket_missing_from_the_base_week_yields_no_change(data):
    """
    Nebraska 6-700 has a price this week and nothing a month back, and Ross's
    sheet prints N/A there. A change against a missing base is not a smaller
    change -- it does not exist, and it must not quietly become one.
    """
    rows = data["states"]["NE"]
    assert ss.cell(rows, WEEK, 600, 700)["price"] == 383.65
    assert ss.cell(rows, BASE, 600, 700) is None
    ws = ss.weeks(data)
    week = next(w for w in ws if w["begin"] == WEEK)
    base = ss.week_containing(ws, ss.month_before(WEEK))
    row = next(r for r in ss.table(data, week, base)
               if r["state"] == "Nebraska" and r["bracket"] == "6-700#")
    assert row["mom"] is None and ss.mom_change(row["mom"]) == ss.NA


# ── published vs quiet ───────────────────────────────────────────────────────

def test_not_published_is_distinct_from_no_qualifying_lots(data):
    """
    Both render N/A, and they are different facts. Nebraska 5-600 published
    and had nothing that qualified; a state that has not filed at all is
    `reported` False, which is what the note under the table reads.
    """
    ws = ss.weeks(data)
    week = next(w for w in ws if w["begin"] == WEEK)
    rows = {(r["state"], r["bracket"]): r for r in ss.table(data, week, None)}
    quiet = rows[("Nebraska", "5-600#")]
    assert quiet["now"] is None and quiet["reported"] is True

    thin = dict(data, states={c: (v if c != "NE" else [])
                              for c, v in data["states"].items()})
    absent = next(r for r in ss.table(thin, week, None)
                  if r["state"] == "Nebraska")
    assert absent["now"] is None and absent["reported"] is False


def test_the_week_picker_opens_on_a_week_every_state_has_filed(data):
    ws = ss.weeks(data)
    every = {c for _, c, _ in ss.STATES}
    assert ss.latest_complete_week(ws)["states"] >= every

    partial = [{"begin": dt.date(2026, 9, 6), "end": dt.date(2026, 9, 12),
                "states": every - {"NE"}}] + ws
    assert ss.latest_complete_week(partial)["begin"] == WEEK
    # ...and with nothing complete it still shows the newest rather than
    # nothing at all.
    assert ss.latest_complete_week(partial[:1])["begin"] == dt.date(2026, 9, 6)


# ── formatting ───────────────────────────────────────────────────────────────

def test_money_prints_whole_dollars_bare_and_the_rest_to_the_cent():
    assert ss.money(328) == "328"
    assert ss.money(382.5) == "382.50"
    assert ss.money(304.50) == "304.50"
    assert ss.money(1234) == "1,234"
    assert ss.money(None) == ss.NA


def test_mom_is_signed_in_both_directions():
    """
    A bare "4.88" leaves a reader telling a rally from a break by the absence
    of a mark -- the correction CLAUDE.md records for the COT line. Up weeks
    and down weeks are close to a coin flip here.
    """
    assert ss.mom_change(-31.94) == "-31.94/cwt"
    assert ss.mom_change(1.49) == "+1.49/cwt"
    assert ss.mom_change(0) == "unchanged"
    assert ss.mom_change(None) == ss.NA


def test_the_week_label_carries_its_year():
    week = {"begin": dt.date(2026, 8, 30), "end": dt.date(2026, 9, 5)}
    assert ss.week_label(week) == "8/30 - 9/5/2026"
    newyear = {"begin": dt.date(2026, 12, 27), "end": dt.date(2027, 1, 2)}
    assert ss.week_label(newyear) == "12/27 - 1/2/2027"


# ── structural ───────────────────────────────────────────────────────────────

def test_the_six_slugs_are_the_ones_ross_named():
    assert {c: s for _n, c, s in ss.STATES} == {
        "AR": 2056, "IA": 2167, "KS": 1895, "MO": 1821, "NE": 1860, "OK": 1831}


def test_this_tab_never_reaches_the_index_tables():
    """
    `six_state.py` is a different population from `mars_sales`/`calf_sales` --
    wider, and with an Arkansas the index roster does not have. It must not
    acquire a database handle, because the moment it can read the index tables
    somebody will average the two.
    """
    import ast

    path = APP_DIR / "six_state.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported == {"datetime", "concurrent", "requests"}, imported

    # By NAME rather than by text, because the module docstring says what
    # these tables are and why this one is not them -- a grep would fail on
    # the explanation of the rule it is enforcing.
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for banned in ("snowflake_db", "db", "get_conn", "calf_sales", "mars_sales"):
        assert banned not in names, f"six_state.py now reaches {banned}"


def test_the_page_passes_the_schema_at_the_call_site():
    """
    Belt and braces over tests/test_cache_schema.py's repo-wide walk, because
    this is the guard that was inert in nineteen loaders at once.
    """
    page = (APP_DIR / "app.py").read_text(
        encoding="utf-8")
    assert "fetch_six_state(MARS_KEY, six_state.SCHEMA)" in page
    assert "_schema" not in page.split("def fetch_six_state")[1][:600]


def test_the_tab_body_is_guarded_so_a_hidden_tab_costs_nothing():
    """
    A hidden Streamlit tab still executes. Without `if tab_six.open:` the six
    MARS requests would run on every load of the Index tab.
    """
    page = (APP_DIR / "app.py").read_text(
        encoding="utf-8")
    body = page.split("with tab_six:")[1]
    assert body.lstrip().startswith("if tab_six.open:")
    assert body.index("fetch_six_state(") > body.index("if tab_six.open:")


def test_the_table_is_rendered_without_the_markdown_parser():
    """
    Eighteen prices in one block. `st.markdown` renders a PAIR of dollar signs
    as inline LaTeX and swallows everything between them -- shipped twice on
    this portal already. `st.html` does not run the markdown parser.
    """
    page = (APP_DIR / "app.py").read_text(
        encoding="utf-8")
    body = page.split("with tab_six:")[1].split("_year = datetime.now().year")[0]
    assert "st.html(" in body
    assert "six-wrap" in body
    for caption in body.split("st.caption(")[1:]:
        head = caption[:600]
        assert "$" not in head.split(")")[0], \
            "a caption on this tab quotes a dollar figure -- escape it or move it"
