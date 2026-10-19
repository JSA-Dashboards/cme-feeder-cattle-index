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
