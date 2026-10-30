"""
Front-month CME Feeder Cattle futures, and their basis to the published index.

SHARED BETWEEN TWO REPOS AND BYTE-IDENTICAL IN BOTH -- this copy and
livestock-portal/apps/cme_feeder_cattle/feeder_futures.py. tests/test_no_drift.py
compares them. Same arrangement as six_state.py, and for the same reason: the
dashboard exists twice and a number that can differ between the copies is a
number nobody can defend.

Self-contained for that reason. It talks to Massive directly rather than
importing apps/livestock_seasonal/massive_api.py, because that module lives in
only one of the two repos and sharing it would put a second file under the
drift test to buy nothing this one needs -- it wants /aggs and nothing else.
test_feeder_futures.py pins the import set, exactly as six_state.py's does.

WHAT BASIS MEANS HERE
---------------------
basis = front-month settle MINUS CME's published Feeder Cattle Index, same day.

Three things make that subtraction legitimate, and each has a wrong-looking
alternative that renders perfectly:

1. THE NEARBY SERIES IS UNADJUSTED. On each date it is the settle of whichever
   contract expires soonest -- a price that actually traded. The usual default
   in charting packages is a BACK-ADJUSTED continuous series, which shifts every
   historical contract so the roll gaps vanish. That series is a fiction carried
   back from today, and subtracting a cash index from a fiction gives a number
   that looks like basis and is not one. A roll gap is a feature here, not a
   defect to smooth away.

2. THE UNITS ALREADY MATCH. CME quotes feeder futures in cents per pound and
   the index in dollars per hundredweight, which are the SAME NUMBER: 351.425
   c/lb = $3.51425/lb = $351.425/cwt. No scaling, and a scale factor applied
   "to be safe" would be a 100x error. Checked live on 2026-10-28, where the
   front month settled 351.425 against an index of 351.43.

3. THE INDEX SIDE IS CME'S OWN PUBLISHED SERIES, not this fleet's
   reconstruction of it. The reconstruction exists to publish an estimate
   before CME does and only reaches back to 2024-02-08; basis measured against
   an estimate of the index is not basis. Callers pass the published series in.

THE SAWTOOTH IS THE CALENDAR, PARTLY
------------------------------------
A cash-settled contract must converge to the index at expiry -- that is what
settlement means -- so nearby basis is widest just after a roll and near zero at
the end of one. Measured over 1,251 sessions to 2026-10-28, mean |basis| by
sessions remaining:

    0-4    1.24        20-29   4.45
    5-9    2.72        30-44   6.19
    10-19  3.66        45+     9.53

Sessions-to-expiry explains r^2 = 0.285 of |basis| but correlates only +0.141
with its SIGN, so the width is substantially calendar and the level is not.
`sessions_to_expiry` is returned on every row so a caller can say so rather than
leaving a reader to read the sawtooth as market noise. `convergence()` is the
check that both series are on the same footing at all: it should sit near zero,
and it did in every year measured (0.41 to 0.99).
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

import requests

#: Massive's futures API. Settlement history is /aggs at resolution 1session.
BASE_URL = "https://api.massive.com/futures/v1"

#: CME feeder cattle. Standard contract months, confirmed from a live /contracts
#: pull: Jan, Mar, Apr, May, Aug, Sep, Oct, Nov.
PRODUCT_CODE = "GF"
MONTH_LETTERS = "FHJKQUVX"
MONTH_ORDER = {letter: i for i, letter in enumerate("FGHJKMNQUVXZ")}

#: How many contract years of tickers to ask for. Massive's daily settlement
#: history begins 2021-09-02, so six years of tickers reaches past the floor and
#: the surplus simply returns nothing -- cheaper than discovering the floor.
YEARS_BACK = 6

#: A contract is "at expiry" inside this many sessions of its last settle. Two,
#: because convergence is what the final print IS; a wider window measures the
#: approach to convergence instead of the thing itself.
EXPIRY_SESSIONS = 1

TIMEOUT = 20


class FuturesError(RuntimeError):
    """Massive could not be reached, or answered with something unusable."""


def contract_tickers(as_of: date, years_back: int = YEARS_BACK) -> list:
    """Every GF ticker that could be front month in the window, newest last.

    Returns (ticker, sort_key) pairs. THE SORT KEY IS DERIVED FROM THE TICKER,
    not from the data, and that matters: ordering legs by each series' last
    observed date -- the obvious alternative -- ties the current front month
    against the one behind it, since both carry a settle for today. A
    (year, month) key cannot tie.
    """
    out = []
    for back in range(years_back):
        year = as_of.year - back
        for letter in MONTH_LETTERS:
            out.append((f"{PRODUCT_CODE}{letter}{year % 10}", (year, MONTH_ORDER[letter])))
    out.sort(key=lambda pair: pair[1])
    return out


def settlement_history(ticker: str, api_key: str, timeout: int = TIMEOUT) -> dict:
    """{date: settle} for one contract, or {} if Massive has nothing for it.

    An empty answer is ORDINARY, not an error: the ticker list deliberately
    reaches past the start of Massive's history, so most of a six-year sweep
    comes back empty on the oldest years.
    """
    try:
        r = requests.get(
            f"{BASE_URL}/aggs/{ticker}",
            headers={"Authorization": f"Bearer {api_key}"},
            params={"resolution": "1session", "limit": 50000},
            timeout=timeout,
        )
    except requests.RequestException as e:
        raise FuturesError(f"{ticker}: {e}") from e
    if r.status_code == 404:
        return {}
    if not r.ok:
        raise FuturesError(f"{ticker}: HTTP {r.status_code}")
    try:
        payload = r.json() or {}
    except ValueError as e:
        raise FuturesError(f"{ticker}: unreadable response") from e
    if payload.get("status") not in ("OK", None):
        raise FuturesError(f"{ticker}: {payload}")

    out = {}
    for bar in payload.get("results") or []:
        day = bar.get("session_end_date")
        if not day:
            continue
        # SETTLEMENT FIRST, CLOSE ONLY AS A FALLBACK, and the `or` is load-bearing
        # rather than defensive: the CURRENT session's bar carries
        # settlement_price 0 until it settles, so reading settlement_price alone
        # drops today and reading close alone silently substitutes a last trade
        # for a settlement on every historical row.
        price = bar.get("settlement_price") or bar.get("close")
        if not price:
            continue
        try:
            when = datetime.fromisoformat(str(day)[:10]).date()
        except ValueError:
            continue
        out[when] = float(price)
    return out


def nearby(api_key: str, as_of: date, years_back: int = YEARS_BACK,
           max_workers: int = 8) -> list:
    """The unadjusted nearby series: one row per session, newest last.

    Each row is {"date", "settle", "contract"}. On every date the settle is that
    of whichever contract expires soonest and has not yet expired. Segments are
    cut at the previous contract's last settle, so no date appears twice and
    nothing is back-adjusted -- see this module's docstring for why that last
    point is the whole basis of the basis.
    """
    if not api_key:
        raise FuturesError("no Massive API key")

    pairs = contract_tickers(as_of, years_back)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        fetched = list(pool.map(lambda p: settlement_history(p[0], api_key), pairs))

    rows, previous_end = {}, None
    for (ticker, _key), history in zip(pairs, fetched):
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


def with_basis(rows: list, index_by_date: dict) -> list:
    """Keep the sessions where both sides exist, and add basis and the calendar.

    Adds "fci_value", "basis" and "sessions_to_expiry". An INNER join, because a
    date with only one of the two cannot have a basis and carrying it forward
    would invent one -- CME publishes on business days only, and the futures feed
    has its own holidays.
    """
    kept = [dict(r, fci_value=index_by_date[r["date"]],
                 basis=r["settle"] - index_by_date[r["date"]])
            for r in rows if r["date"] in index_by_date]

    remaining = {}
    for row in reversed(kept):
        remaining[row["contract"]] = remaining.get(row["contract"], -1) + 1
        row["sessions_to_expiry"] = remaining[row["contract"]]
    return kept


def convergence(rows: list) -> float:
    """Mean |basis| in each contract's final sessions, or None if none qualify.

    THE CHECK THAT BOTH SERIES ARE ON THE SAME FOOTING. A cash-settled contract
    must converge, so this sits near zero when the units, the roll and the index
    series are all right, and blows up when any one of them is wrong. It is the
    one number here that is an assertion about the pipeline rather than about
    the market -- and it is cheap, so show it.
    """
    at_expiry = [abs(r["basis"]) for r in rows if r["sessions_to_expiry"] <= EXPIRY_SESSIONS]
    return sum(at_expiry) / len(at_expiry) if at_expiry else None


def summary(rows: list) -> dict:
    """Headline figures for the panel. Returns {} on an empty series."""
    if not rows:
        return {}
    values = [r["basis"] for r in rows]
    by_year = {}
    for r in rows:
        by_year.setdefault(r["date"].year, []).append(r["basis"])
    return {
        "sessions": len(rows),
        "start": rows[0]["date"],
        "end": rows[-1]["date"],
        "latest": values[-1],
        "mean": sum(values) / len(values),
        "low": min(values),
        "high": max(values),
        "over_index_pct": 100.0 * sum(1 for v in values if v > 0) / len(values),
        "convergence": convergence(rows),
        "by_year": {y: sum(v) / len(v) for y, v in sorted(by_year.items())},
        "rolls": sum(1 for a, b in zip(rows, rows[1:]) if a["contract"] != b["contract"]),
    }
