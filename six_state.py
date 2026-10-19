"""
Six-state weekly feeder steer prices -- the table Ross builds by hand.

Medium and Large 1 steers, by weight bracket, for Arkansas, Iowa, Kansas,
Missouri, Nebraska and Oklahoma, from the six AMS weekly auction summaries:

    2056  Arkansas Weekly Livestock Auction Summary
    2167  Iowa Weekly Cattle Auction Summary
    1895  Kansas Weekly Cattle Auction Summary
    1821  Missouri Weekly Cattle Auction Summary
    1860  Nebraska Weekly Livestock Auction Summary
    1831  Oklahoma Weekly Cattle Auction Summary

THIS IS NOT THE PAGE'S OWN DATA AND MUST NOT BE CONFUSED WITH IT. The rest of
the CME Feeder Cattle Index dashboard reads `mars_sales` / `calf_sales`, which
are ingested from a 93-barn roster across TWELVE states -- and that roster has
no Arkansas barn in it at all. These six state summaries are USDA's own
state-level roll-ups of every reporting auction in the state, which is a wider
and differently-drawn population. Two figures for "Kansas 700-899 lb steers"
can therefore both be right and disagree, which is the letter-versus-dashboard
failure CLAUDE.md records three times. Nothing here feeds the index, and
nothing here is averaged against a figure from `calf_sales`.

THE ARITHMETIC WAS REVERSE-ENGINEERED FROM ROSS'S OWN TABLE, NOT GUESSED.

Given his screenshot for the week of 8/30-9/5/2026, every rule below was
settled by reproducing his printed figures off the live MARS rows:

  * Price Range reproduces on 17 of 17 cells.
  * WTD average price reproduces to the CENT on 15 of 17.
  * WTD average weight reproduces on 14 of 16 readable cells.
  * MoM Change reproduces to the cent on 12 of 16.

Every miss is in Arkansas (3 cells) or Iowa 7-899 (1 cell), and both are
explained by AMS revising rows after he took the screenshot rather than by a
different rule: a brute-force search over all subsets of the Arkansas rows for
that week found a UNIQUE subset reproducing each of his two figures, and in
both cases it excludes single-head lots that are in the feed today. Applying
that exclusion as a rule breaks Oklahoma 5-600, so it is not the rule -- the
data moved. AMS restates these summaries; see `published` on each state.

The rules, each of which fails silently if got wrong:

  * class Steers, frame "Medium and Large", muscle_grade exactly "1".
    NOT "1-2". Including 1-2 moves Missouri 6-700 from 373.11 to 357.40 --
    plausible, different, and nothing raises. "Includes Special Conditions"
    in the title means the lot descriptors (Value Added, Unweaned, Fleshy,
    Thin Fleshed, Fancy, Full ...) are all KEPT; it is not a filter.
  * commodity "Feeder Cattle". The same class/frame/grade also appears under
    Replacement Cattle and Slaughter Cattle.
  * price_unit "Per Cwt". Small lots are quoted PER ANIMAL, and AMS puts that
    number in the same `avg_price` column -- the mixed-units trap
    `calf_sales.py` documents, where a per-head price stored as $/cwt made one
    barn the chart leader at $1,118.70/cwt. These rows are DROPPED rather than
    converted: they are a few head against a state roll-up, and converting
    them would put a figure on this page that USDA's own report does not.
  * Both averages are weighted by HEAD, not by pounds. The rest of this
    dashboard is pound-weighted (see `cash_calves.py`), so this is the one
    place on the page that is not, and it is what the source table does:
    pound-weighting moves Oklahoma 7-899 from 324.31 to 323.79 against a
    printed 324.31.
  * MoM Change subtracts the two ROUNDED averages, never the raw ones. That
    is the convention CLAUDE.md settled for the FCI after the letter and the
    dashboard disagreed by a cent -- a reader holding both weeks must be able
    to subtract the printed numbers and get the printed change. Oklahoma
    5-600 prints -39.07; the unrounded difference is -39.0766, which rounds
    to -39.08.
  * The MoM base is THE WEEK CONTAINING THE DATE ONE MONTH BEFORE THIS WEEK'S
    FIRST DAY -- for the week beginning 8/30 that is 7/30, so the week of
    7/26-8/1. Four weeks back (8/2) reproduces none of his figures; this rule
    reproduces twelve. It is a CALENDAR month rather than a fixed number of
    weeks, so the page names the base week rather than leaving a reader to
    count backwards.

A STATE THAT HAS NOT PUBLISHED IS NOT A STATE WITH NO SALES. Nebraska runs
about a week behind the other five and Iowa published nothing at all for the
week of 7/19/2026. Those are different facts from "no qualifying lots this
week", so `cell()` returns None for both and `table()` carries `published` so
the page can say which it is. Defaulting the picker to the newest week ALL SIX
have published is what keeps the table complete enough to screenshot.
"""
import datetime as dt
from concurrent.futures import ThreadPoolExecutor

import requests

# Bump whenever fetch() or table() changes the SHAPE of what it returns.
# `st.cache_data` keys on the decorated function's own code and arguments and
# never on the modules it calls, so without this the page keeps serving a dict
# from before the change and the new key renders an em dash with nothing
# raising -- the `leverage.SCHEMA` trap in CLAUDE.md. It is passed in at the
# CALL SITE: a default is not part of the key.
SCHEMA = 1

MARS_BASE = "https://marsapi.ams.usda.gov/services/v1.2/reports"
REPORT_VIEW = "https://mymarketnews.ams.usda.gov/viewReport/{}"

# (display name, postal code, MARS slug). Order is the order on the page:
# alphabetical, left column then right, exactly as Ross's table is laid out.
STATES = (
    ("Arkansas", "AR", 2056),
    ("Iowa", "IA", 2167),
    ("Kansas", "KS", 1895),
    ("Missouri", "MO", 1821),
    ("Nebraska", "NE", 1860),
    ("Oklahoma", "OK", 1831),
)

# (label, weight_break_low inclusive, exclusive). AMS breaks these summaries
# into 50 lb bands, so each bracket is several bands: 7-899# is 700/750/800/850.
BRACKETS = (
    ("5-600#", 500, 600),
    ("6-700#", 600, 700),
    ("7-899#", 700, 900),
)

TITLE = "Medium and Large 1 Steers- Includes Special Conditions"

CLASS = "Steers"
FRAME = "Medium and Large"
GRADE = "1"
COMMODITY = "Feeder Cattle"
PER_CWT = "Per Cwt"

# Long enough to fill the week picker and still reach the MoM base week five
# weeks behind the oldest selectable one. Server-side filtering on
# class/frame/muscle_grade keeps this to about 4,300 rows and 9 MB across the
# six slugs -- unfiltered it is 31,000 rows and 69 MB, almost all of it the
# report narrative repeated on every row.
HISTORY_DAYS = 182

NA = "N/A"

# How far the as-reported average and the fixed-panel one must disagree
# before the summary says so, $/cwt. Below this the two tell the same
# story, and a caution that fires every week is one nobody reads on the
# week it matters.
ROSTER_MATERIAL = 2.00

# Weeks that must be on file before calling one of them the widest or
# narrowest spread means anything. On a short file every week is an
# extreme of it -- with two weeks the caution fires on BOTH, which is a
# banner that always fires and so is one nobody reads on the week it
# matters. 182 days of history is 26 weeks, so this costs nothing in
# normal running and only withholds the claim when it would be vacuous.
MIN_WEEKS_FOR_EXTREME = 8


def _num(v):
    """AMS sends numbers as strings. Blank / NA / unparseable -> None."""
    if v is None:
        return None
    t = str(v).strip().replace(",", "").replace("$", "")
    if not t or t.upper() in ("NA", "N/A", "NULL", "-"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _mdy(s):
    """MM/DD/YYYY, sometimes with a trailing clock time -> date, else None."""
    if not s:
        return None
    try:
        m, d, y = str(s).split()[0].split("/")
        return dt.date(int(y), int(m), int(d))
    except Exception:
        return None


def _trim(r):
    """
    One MARS row cut down to what this page reads.

    The payload is trimmed before it is cached because `report_narrative` is a
    multi-kilobyte block repeated on every row -- it is 90% of the response and
    nothing here reads it.
    """
    return {
        "begin": _mdy(r.get("report_begin_date")),
        "end": _mdy(r.get("report_end_date")),
        "published": _mdy(r.get("published_date")),
        "commodity": r.get("commodity"),
        "class": r.get("class"),
        "frame": r.get("frame"),
        "grade": r.get("muscle_grade"),
        "unit": r.get("price_unit"),
        "lot_desc": r.get("lot_desc"),
        "wlow": _num(r.get("weight_break_low")),
        "head": _num(r.get("head_count")),
        "weight": _num(r.get("avg_weight")),
        "price": _num(r.get("avg_price")),
        "price_low": _num(r.get("avg_price_min")),
        "price_high": _num(r.get("avg_price_max")),
    }


def _fetch_one(key, slug, days):
    """One state. Returns (rows, error) -- never raises."""
    end = dt.date.today()
    start = end - dt.timedelta(days=days)
    # Filtered server side as well as below. The duplication is deliberate:
    # the server filter is what makes the request affordable, and the client
    # filter in `_qualifying` is what a test can read and what keeps the page
    # honest if AMS ever loosens its matching. Verified 2026-10-19 that the
    # two select exactly the same rows across all six states.
    q = (f"report_date={start:%m/%d/%Y}:{end:%m/%d/%Y}"
         f";class={CLASS};frame={FRAME};muscle_grade={GRADE}")
    try:
        r = requests.get(f"{MARS_BASE}/{slug}", auth=(key, ""),
                         params={"q": q}, timeout=(5, 180))
        if r.status_code in (204, 404):
            return [], f"AMS returned HTTP {r.status_code}"
        r.raise_for_status()
        return [_trim(x) for x in (r.json().get("results") or [])], None
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"


def fetch(key: str, days: int = HISTORY_DAYS) -> dict:
    """
    All six state summaries.

    Returns {"states": {code: [row]}, "errors": {code: msg}}. Never raises --
    a dead feed should say so on the page rather than blank it, and one state
    failing must not cost the other five.
    """
    if not key:
        return {"states": {}, "errors": {c: "MARS_API_KEY not configured"
                                         for _, c, _ in STATES}}
    # Six independent requests; serially they are about 25 seconds and in
    # parallel about 6. The same shape `apps/market_board/loaders.py` uses.
    with ThreadPoolExecutor(max_workers=len(STATES)) as ex:
        futures = {code: ex.submit(_fetch_one, key, slug, days)
                   for _, code, slug in STATES}
        done = {code: f.result() for code, f in futures.items()}
    return {"states": {c: rows for c, (rows, _) in done.items()},
            "errors": {c: err for c, (_, err) in done.items() if err}}


def _qualifying(rows, begin, low, high):
    """The rows one cell is computed from. Every clause here is load-bearing."""
    out = []
    for r in rows:
        if r["begin"] != begin:
            continue
        if r["commodity"] != COMMODITY or r["class"] != CLASS:
            continue
        if r["frame"] != FRAME or r["grade"] != GRADE:
            continue
        if r["unit"] != PER_CWT:
            continue
        w = r["wlow"]
        if w is None or not (low <= w < high):
            continue
        if not (r["head"] and r["weight"] and r["price"]):
            continue
        out.append(r)
    return out


def cell(rows, begin, low, high):
    """
    One state / one bracket / one week, or None if nothing qualified.

    None is "no qualifying lots", which the caller must keep distinct from
    "this state has not published this week yet" -- see the module note.
    """
    rs = _qualifying(rows, begin, low, high)
    if not rs:
        return None
    head = sum(r["head"] for r in rs)
    if not head:
        return None
    return {
        "head": int(head),
        "lots": len(rs),
        # Rounded here, once, because the MoM change subtracts the PRINTED
        # figures. Rounding at render time instead would let the change and
        # the two levels it sits between disagree by a cent.
        "weight": round(sum(r["head"] * r["weight"] for r in rs) / head),
        "price": round(sum(r["head"] * r["price"] for r in rs) / head, 2),
        "low": min((r["price_low"] if r["price_low"] is not None else r["price"])
                   for r in rs),
        "high": max((r["price_high"] if r["price_high"] is not None else r["price"])
                    for r in rs),
    }


def weeks(data: dict):
    """
    Every week in the pull, newest first.

    Each entry is {"begin", "end", "states": {codes that published}}. A state
    is counted as having published the week if it has any row dated to it at
    all -- including rows this page filters out, because the question that
    answers is "has AMS put this week out", not "did it hold qualifying lots".
    """
    seen = {}
    for code, rows in (data.get("states") or {}).items():
        for r in rows:
            b = r["begin"]
            if b is None:
                continue
            w = seen.setdefault(b, {"begin": b, "end": r["end"], "states": set()})
            w["states"].add(code)
            if w["end"] is None:
                w["end"] = r["end"]
    return sorted(seen.values(), key=lambda w: w["begin"], reverse=True)


def month_before(d: dt.date) -> dt.date:
    """
    One CALENDAR month earlier, clamped to the end of a shorter month.

    Month-over-month means a month, not a fixed number of weeks: for the week
    beginning 8/30 this is 7/30, whose week begins 7/26. Four weeks back is
    8/2 and reproduces none of Ross's printed changes.
    """
    year, month = (d.year, d.month - 1) if d.month > 1 else (d.year - 1, 12)
    day = d.day
    while day > 28:
        try:
            return dt.date(year, month, day)
        except ValueError:
            day -= 1
    return dt.date(year, month, day)


def week_containing(ws, day):
    """The published week whose span covers `day`, or None."""
    if day is None:
        return None
    for w in ws:
        if w["end"] is not None and w["begin"] <= day <= w["end"]:
            return w
        # A week whose end date never came through still spans Sun-Sat.
        if w["end"] is None and w["begin"] <= day <= w["begin"] + dt.timedelta(days=6):
            return w
    return None


def latest_complete_week(ws):
    """
    The newest week every state has published, else the newest week at all.

    These six offices publish four to eight days after the week ends and
    Nebraska is routinely last, so the newest week on the wire is usually five
    states wide. A table with a blank column is not what this page is for.
    """
    if not ws:
        return None
    every = {c for _, c, _ in STATES}
    for w in ws:
        if w["states"] >= every:
            return w
    return ws[0]


def table(data: dict, week, base_week=None):
    """
    The eighteen cells, in page order.

    Each row: {"state", "code", "bracket", "now", "base", "mom", "published",
    "reported"}. `reported` is whether the state published this week at all;
    `now` is None when it published and nothing qualified. `mom` is None unless
    BOTH weeks have a figure -- a change against a missing base is not a
    smaller change, it is no change at all.
    """
    states = data.get("states") or {}
    published = {w["begin"]: w["states"] for w in weeks(data)}
    out = []
    for name, code, _slug in STATES:
        rows = states.get(code) or []
        reported = week is not None and code in published.get(week["begin"], set())
        last = max((r["published"] for r in rows if r["published"]), default=None)
        for label, low, high in BRACKETS:
            now = cell(rows, week["begin"], low, high) if week else None
            base = (cell(rows, base_week["begin"], low, high)
                    if base_week else None)
            out.append({
                "state": name,
                "code": code,
                "bracket": label,
                "now": now,
                "base": base,
                "mom": (round(now["price"] - base["price"], 2)
                        if now and base else None),
                "reported": reported,
                "published": last,
            })
    return out


# ── Formatting ───────────────────────────────────────────────────────────────
# These live here rather than in the page because they are part of the claim
# the table makes, and because the tests pin them against Ross's own printed
# strings. Nothing below emits HTML.

def money(v) -> str:
    """
    USDA's own style on these reports: a whole dollar prints bare, anything
    else to the cent. 328 -> "328", 382.5 -> "382.50", 304.5 -> "304.50".
    """
    if v is None:
        return NA
    return f"{v:,.0f}" if float(v) == int(float(v)) else f"{v:,.2f}"


def price_range(c) -> str:
    """"328 - 423". A lone print still shows both ends, as USDA's reports do."""
    if not c:
        return NA
    return f"{money(c['low'])} - {money(c['high'])}"


def wtd_average(c) -> str:
    """"551# @ $373.77" -- the average weight the average price was made on."""
    if not c:
        return NA
    return f"{c['weight']:,.0f}# @ ${c['price']:,.2f}"


def mom_change(v) -> str:
    """
    "-31.94/cwt", "+1.49/cwt". Signed in BOTH directions, because a change has
    a direction a level does not and a bare "4.88" leaves the reader telling a
    rally from a break by the absence of a mark -- the correction CLAUDE.md
    records for the COT week-over-week line. An exactly unchanged week says so
    in words rather than printing "+0.00".
    """
    if v is None:
        return NA
    if v == 0:
        return "unchanged"
    return f"{v:+,.2f}/cwt"


def week_label(week) -> str:
    """"8/30 - 9/5/2026" -- M/D, with the year on the end so a screenshot of
    this table still says which year it is, and so a week spanning New Year
    is not ambiguous."""
    if not week:
        return NA
    b, e = week["begin"], week["end"] or week["begin"] + dt.timedelta(days=6)
    return f"{b.month}/{b.day} - {e.month}/{e.day}/{e.year}"


# ── The market summary ───────────────────────────────────────────────────────
# WHAT moved, and never why. The cause is written by a human on the page and
# saved beside the week -- the arithmetic/editorial split the daily letter
# already runs on, and the reason CLAUDE.md gives for having removed a
# plausible wrong cause from the beef-trade chart.
#
# Nothing here reaches a second data source. Corn and the feeder board would
# both be relevant and neither is here, because a test pins this module's
# imports to exactly {datetime, concurrent, requests} to stop it acquiring a
# database handle. That is also why `_median` is written out below rather than
# imported from `statistics`.

def _median(xs):
    """The median of a list, or None. See the import note above."""
    s = sorted(x for x in xs if x is not None)
    if not s:
        return None
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


def _hw(cells, bracket, states=None):
    """(head-weighted price, head) over one bracket, optionally one panel."""
    num = den = 0
    for (st, br), c in cells.items():
        if br != bracket or not c or (states is not None and st not in states):
            continue
        num += c["price"] * c["head"]
        den += c["head"]
    return (round(num / den, 2) if den else None), den


def _cells(data, week):
    """{(state, bracket): cell} for one week, qualifying lots only."""
    if not week:
        return {}
    return {(r["state"], r["bracket"]): r["now"]
            for r in table(data, week) if r["now"]}


def _priced(cells, bracket):
    return {st for (st, br), c in cells.items() if br == bracket and c}


def bracket_move(data: dict, week, other):
    """
    Each bracket's move between two weeks, as reported AND on a fixed panel.

    THE TWO ARE DIFFERENT NUMBERS AND THE DIFFERENCE IS THE WHOLE POINT. A
    state entering or leaving a bracket moves the head-weighted average with
    no barn changing its bid. Nebraska went from none of the 5-600# head in
    the week of 8/30/2026 to 62% of it by 10/11, and its light calves are the
    dearest in the panel -- on its own that accounted for +27.45 of what
    looked like a +49.56 move, 55% of it.

    The eighteen published cells compare a state against ITSELF and so carry
    none of this. An average across states carries all of it, which is why
    `same_panel` is what the summary leads with and `reported` is kept beside
    it rather than thrown away.

    Returns {bracket: {"level", "reported", "same_panel", "roster", "panel",
    "head"}}, with None where either week lacks the bracket.
    """
    a, b = _cells(data, other), _cells(data, week)
    out = {}
    for label, _low, _high in BRACKETS:
        lvl, head = _hw(b, label)
        ra, _ = _hw(a, label)
        rb = lvl
        panel = _priced(a, label) & _priced(b, label)
        pa, _ = _hw(a, label, panel)
        pb, _ = _hw(b, label, panel)
        reported = round(rb - ra, 2) if (ra is not None and rb is not None) else None
        same = round(pb - pa, 2) if (pa is not None and pb is not None) else None
        out[label] = {
            "level": lvl,
            "head": head,
            "reported": reported,
            "same_panel": same,
            "roster": (round(reported - same, 2)
                       if (reported is not None and same is not None) else None),
            "panel": sorted(panel),
        }
    return out


def slide(data: dict, week):
    """
    Light minus heavy for one week: the median ACROSS STATES, not the
    difference of two head-weighted averages.

    The median is the robust one here for the reason in `bracket_move`: the
    two ends of the ladder are not drawn from the same roster in a given week,
    so differencing two head-weighted averages measures the roster as much as
    the ladder. Over the three weeks to 10/17/2026 the head-weighted spread
    moved 91.80 -> 97.50 -> 92.90 while this one sat at 80.88 -> 80.42 ->
    80.81; the flat series is the honest one.

    None unless at least one state priced BOTH ends.
    """
    c = _cells(data, week)
    lo_label, hi_label = BRACKETS[0][0], BRACKETS[-1][0]
    gaps = [c[(st, lo_label)]["price"] - c[(st, hi_label)]["price"]
            for st in _priced(c, lo_label) & _priced(c, hi_label)]
    v = _median(gaps)
    return None if v is None else round(v, 2)


def slide_history(data: dict):
    """[(week, slide)] oldest first, over every week on file that has one."""
    out = [(w, slide(data, w)) for w in sorted(weeks(data), key=lambda w: w["begin"])]
    return [(w, s) for w, s in out if s is not None]


def movers(data: dict, week, other, bracket):
    """
    [(state, change)] for one bracket between two weeks, biggest first.

    Per state against itself, so this is the published cell's arithmetic and
    carries no roster effect.
    """
    a, b = _cells(data, other), _cells(data, week)
    out = [(st, round(b[(st, bracket)]["price"] - a[(st, bracket)]["price"], 2))
           for st in _priced(a, bracket) & _priced(b, bracket)]
    return sorted(out, key=lambda t: t[1], reverse=True)


def summary(data: dict, week, prev_week=None, base_week=None) -> dict:
    """
    Everything the page states about the week, computed. No cause anywhere.

    `split` is the headline: how far the light end moved against the heavy end
    on a FIXED panel, which is the question "are lighter calves bringing a
    bigger premium" actually asks. `base_extreme` fires when the week being
    compared against is itself the widest or narrowest on file, because a
    month change measured off an outlier describes the outlier.
    """
    lo_label, hi_label = BRACKETS[0][0], BRACKETS[-1][0]
    hist = slide_history(data)
    vals = [s for _w, s in hist]
    here = slide(data, week)

    out = {
        "week": week,
        "prev_week": prev_week,
        "base_week": base_week,
        "slide": here,
        "slide_prev": slide(data, prev_week) if prev_week else None,
        "slide_base": slide(data, base_week) if base_week else None,
        "slide_min": min(vals) if vals else None,
        "slide_max": max(vals) if vals else None,
        "weeks_on_file": len(hist),
        "wow": bracket_move(data, week, prev_week) if prev_week else {},
        "mom": bracket_move(data, week, base_week) if base_week else {},
        "base_extreme": None,
    }

    for span in ("wow", "mom"):
        m = out[span]
        lo = m.get(lo_label, {}).get("same_panel")
        hi = m.get(hi_label, {}).get("same_panel")
        out[span + "_split"] = (round(lo - hi, 2)
                                if (lo is not None and hi is not None) else None)

    sb = out["slide_base"]
    if sb is not None and len(vals) >= MIN_WEEKS_FOR_EXTREME:
        if sb == min(vals):
            out["base_extreme"] = "narrowest"
        elif sb == max(vals):
            out["base_extreme"] = "widest"

    out["leaders"] = {
        span: ({b[0]: movers(data, week, other, b[0]) for b in BRACKETS}
               if other else {})
        for span, other in (("mom", base_week), ("wow", prev_week))}
    return out


def split_sentence(s: dict, span: str = "mom") -> str:
    """
    The one line the whole panel exists to produce, written so it reads on its
    own -- CLAUDE.md's standing rule. It names both brackets, both figures,
    the period and the panel it was measured on, because "lighter calves led"
    is the claim a reader would otherwise have to take on trust.
    """
    lo_label, hi_label = BRACKETS[0][0], BRACKETS[-1][0]
    m, split = s.get(span) or {}, s.get(span + "_split")
    other = s.get("base_week" if span == "mom" else "prev_week")
    word = "month" if span == "mom" else "week"
    if split is None or not m:
        return (f"No {word}-over-{word} comparison: the {word}-ago week is not "
                f"on file, or no state priced both ends of the ladder in it.")
    lo, hi = m[lo_label]["same_panel"], m[hi_label]["same_panel"]
    n = len(m[lo_label]["panel"])
    lead = (f"{lo_label} calves outran {hi_label} by {abs(split):,.2f}/cwt"
            if split > 0 else
            f"{hi_label} cattle outran {lo_label} by {abs(split):,.2f}/cwt"
            if split < 0 else
            f"{lo_label} and {hi_label} moved together")
    return (f"Against the week of {week_label(other)}, {lead} "
            f"({lo_label} {mom_change(lo)}, {hi_label} {mom_change(hi)}), "
            f"measured on the {n} state{'' if n == 1 else 's'} that priced "
            f"that bracket in both weeks.")


def roster_caution(s: dict, span: str = "mom"):
    """
    The sentence to print when the as-reported average and the fixed-panel one
    disagree materially, naming both. None when they agree.

    `ROSTER_MATERIAL` is 2.00/cwt: below that the two readings tell the same
    story and a caution on every load is one nobody reads on the week it
    matters -- the argument `reconciles()` already makes for not crying stale
    at a weekend.
    """
    lo_label = BRACKETS[0][0]
    m = (s.get(span) or {}).get(lo_label)
    if not m or m.get("roster") is None:
        return None
    if abs(m["roster"]) < ROSTER_MATERIAL:
        return None
    return (f"Read that on the fixed panel, not the raw average: across all "
            f"reporting states {lo_label} shows {mom_change(m['reported'])}, "
            f"of which {m['roster']:+,.2f} is which states brought light "
            f"calves rather than any bid moving.")


def base_caution(s: dict):
    """The month base being an extreme of the series, or None."""
    if not s.get("base_extreme"):
        return None
    return (f"The month-ago week of {week_label(s['base_week'])} is the "
            f"{s['base_extreme']} light-to-heavy spread of the "
            f"{s['weeks_on_file']} weeks on file, at "
            f"${s['slide_base']:,.2f}/cwt, so the month change is measured "
            f"off an outlier and overstates the move.")


def slide_sentence(s: dict) -> str:
    """Where the spread sits now against everything on file."""
    if s.get("slide") is None:
        return "No state priced both ends of the ladder this week."
    bits = [f"The light-to-heavy spread is ${s['slide']:,.2f}/cwt"]
    if s.get("slide_base") is not None:
        bits.append(f"against ${s['slide_base']:,.2f} a month ago")
    if s.get("slide_min") is not None and s.get("slide_max") is not None:
        bits.append(f"within a range of ${s['slide_min']:,.2f} to "
                    f"${s['slide_max']:,.2f} over the {s['weeks_on_file']} "
                    f"weeks on file")
    return ", ".join(bits) + "."


def leader_sentence(s: dict, span: str = "mom") -> str:
    """
    Which states led and which went the other way, named, with their figures.

    NOT DECORATION. Over the month to 10/10/2026 the light end outran the
    heavy end in Iowa, Kansas and Missouri and LOST to it in Arkansas and
    Oklahoma, so "lighter calves led" was false in two of the five states on
    the table. A panel figure alone hides that completely.

    It names its own period and both figures because the line is quotable on
    its own -- CLAUDE.md's standing rule, and the reason this does not read
    "Iowa led" and stop.
    """
    lo_label, hi_label = BRACKETS[0][0], BRACKETS[-1][0]
    heads = (s.get("leaders") or {}).get(span) or {}
    lo = dict(heads.get(lo_label) or [])
    hi = dict(heads.get(hi_label) or [])
    both = {st: round(lo[st] - hi[st], 2) for st in lo if st in hi}
    if not both:
        return ""
    word = "month" if span == "mom" else "week"
    led = sorted(both.items(), key=lambda t: t[1])
    parts = []
    top, gap = led[-1]
    if gap > 0:
        parts.append(f"Over the {word}, {top} split the widest: its "
                     f"{lo_label} {mom_change(lo[top])} against "
                     f"{mom_change(hi[top])} on its {hi_label}")
    against = [k for k, v in led if v < 0]
    if against:
        parts.append("the heavy end outran the light end in "
                     + ", ".join(against))
    return "; ".join(parts) + "." if parts else ""
