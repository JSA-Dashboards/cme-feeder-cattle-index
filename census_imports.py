"""
Census International Trade API: live cattle imported from Mexico, monthly.

WHY THIS SOURCE AT ALL. AMS's border reports (border_reports.py) carry the trade
STATUS and port commentary but zero structured numbers -- all seven of AMS's
International Livestock reports return empty data arrays through MARS. Census is
where the actual head counts live, because every animal crossing the border is a
customs entry.

TWO THINGS THAT WILL BITE.

1. CENSUS LAGS ABOUT SIX WEEKS. January data publishes in early March. So this
   series can never answer "what crossed last week" -- that is what the AMS
   reporting-day proxy is for. Every consumer of this module must show the
   data-through month, which is why monthly_head() returns it rather than
   leaving the caller to assume "current".

2. THE HS10 CODE LIST IS NOT STABLE AND IS NOT GUESSABLE. Live bovine animals
   sit under HS 0102, but the ten-digit breakouts (weight bands, immediate
   slaughter vs not, dairy cows, purebred breeding) get renumbered, and a code
   that carries all the feeder volume one year can be empty the next. Hardcoding
   a code list is exactly the failure mode that dropped 22,709 replacement-sale
   rows when AMS renamed "Per Unit" to "Per Head" in 2022. So discover() asks
   Census which 0102 codes actually returned data for Mexico in a given period
   and the ingest stores every one of them with its description. Classification
   into feeder / slaughter / breeding happens at READ time, in
   classify_commodity(), where it can be corrected without a re-pull.

UNITS. For live animals Census reports quantity in UNIT_QY1 = "NO." (number of
head). That is checked on ingest rather than assumed -- if Census ever reports a
weight unit instead, unit_qy1 preserves it and the check fires loudly instead of
silently recording kilograms as head.

GENERAL vs CONSUMPTION imports. GEN_* is everything that physically arrived;
CON_* is what cleared into US commerce. For live cattle they are all but
identical (nothing goes into a bonded warehouse), and "crossed the border" is
the question this page asks, so this module stores GEN_ and ignores CON_.
"""
import argparse
import os
from datetime import date

import requests
from dotenv import load_dotenv

load_dotenv()

import snowflake_db as db

# The timeseries endpoints. porths gives the same numbers broken out by district
# and port of entry, which is what lets the page tie a volume to the crossings
# AMS writes about (Douglas AZ, Santa Teresa NM, Eagle Pass TX).
NATIONAL_URL = "https://api.census.gov/data/timeseries/intltrade/imports/hs"
PORT_URL = "https://api.census.gov/data/timeseries/intltrade/imports/porths"

MEXICO_CTY = "2010"     # Census country code, not an ISO code
HS_LIVE_BOVINE = "0102"

COLUMNS = ["period", "commodity", "descr", "port_code", "port_name",
           "head", "value_usd", "unit_qy1"]


def init_tables(conn):
    # port_code is '' for the national row rather than NULL: it is part of the
    # primary key, and NULL never equals NULL in a MERGE ON clause, so a NULL
    # here would make every national row insert as a duplicate on Snowflake
    # while behaving fine on SQLite.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS census_cattle_imports (
            period TEXT NOT NULL,
            commodity TEXT NOT NULL,
            descr TEXT,
            port_code TEXT NOT NULL,
            port_name TEXT,
            head BIGINT,
            value_usd BIGINT,
            unit_qy1 TEXT,
            PRIMARY KEY (period, commodity, port_code)
        )
    """)
    conn.commit()


def get_key():
    k = os.environ.get("CENSUS_API_KEY", "").strip()
    if not k:
        raise SystemExit(
            "CENSUS_API_KEY is not set.\n"
            "Add a line to .env in this directory:  CENSUS_API_KEY=<your key>\n"
            "Request one at https://api.census.gov/data/key_signup.html")
    return k


def _get(url, params):
    """
    Census returns a header row followed by data rows, or 204 with an empty body
    when a query matches nothing. An empty result is normal here -- a month with
    no cattle crossings is the whole point of watching this series -- so it comes
    back as [] rather than raising.
    """
    p = dict(params)
    p["key"] = get_key()
    r = requests.get(url, params=p, timeout=(5, 90))
    if r.status_code in (204, 404):
        return []
    if r.status_code >= 400:
        raise RuntimeError(f"Census {r.status_code}: {r.text[:300]}")
    rows = r.json()
    if not rows or len(rows) < 2:
        return []
    head, *data = rows
    return [dict(zip(head, row)) for row in data]


def _num(v):
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return None
    # Census uses negative values for adjustments/revisions on some series;
    # keep them rather than clamping, but treat the missing-data sentinel as null.
    return None if n <= -999999999 else n


def discover(period: str):
    """
    Which HS10 codes under 0102 actually carried Mexican volume in `period`.

    Returns [(commodity, description, head, unit)] sorted by head descending.
    Use this to see the vocabulary before trusting any classification of it.
    """
    rows = _get(NATIONAL_URL, {
        "get": "I_COMMODITY,I_COMMODITY_LDESC,GEN_QY1_MO,GEN_VAL_MO,UNIT_QY1",
        "time": period,
        "CTY_CODE": MEXICO_CTY,
        "COMM_LVL": "HS10",
        "I_COMMODITY": HS_LIVE_BOVINE + "*",
    })
    out = [(r.get("I_COMMODITY"), r.get("I_COMMODITY_LDESC"),
            _num(r.get("GEN_QY1_MO")), r.get("UNIT_QY1"))
           for r in rows]
    return sorted(out, key=lambda t: -(t[2] or 0))


def classify_commodity(commodity: str, descr: str) -> str:
    """
    Bucket an HS10 line into feeder / slaughter / breeding / dairy / other.

    Deliberately keyed off the DESCRIPTION, not the code number, because the
    codes get renumbered and the descriptions do not. Read-time classification
    means a wrong call here is a one-line fix, not a re-pull.

    THE NEGATION TRAP, which this got wrong twice before it got it right. Every
    commercial line carries an EXCLUSION clause:

        CATTLE, LIVE, MALE, WEIGHING 200 KG OR MORE BUT LESS THAN 320 KG EACH,
        OTHER THAN PUREBRED BREEDING AND/OR DAIRY

    so a substring test for "breeding" labels the entire feeder volume as
    breeding stock, and fixing that by testing "dairy" instead labels it dairy.
    In June 2024 either mistake mislabelled 115,636 of 116,696 head -- a 99%
    error that still rendered a completely plausible-looking table.

    The fix is structural rather than another special case: everything after
    "OTHER THAN" says what the line is NOT, so it is split off and only the
    positive part is matched. Any future exclusion Census adds is then handled
    automatically instead of needing a new negative test.

    "Feeder" here means commercial cattle crossing to be grown out: not for
    immediate slaughter, not breeding stock, not dairy. That matches how the
    trade quotes "Mexican feeder imports" (~1.25m head in 2024) and is the
    bucket that competes with US calves. weight_band() splits it further.
    """
    positive, _, _excluded = (descr or "").upper().partition("OTHER THAN")
    if "IMMEDIATE SLAUGHTER" in positive:
        return "slaughter"
    if "DAIRY" in positive:
        return "dairy"
    if "PUREBRED" in positive or "FOR BREEDING" in positive:
        return "breeding"
    # Weight bands. Mexican cattle cross light and go to grass or a yard, so
    # any banded commercial line is feeder volume regardless of the band.
    if "WEIGHING" in positive and "KG" in positive:
        return "feeder"
    return "other"


def weight_band(descr: str) -> str:
    """
    The HS weight band as a short label, in pounds as well as kilos.

    Worth having because it lines up with the index's own bracket work: 320 kg
    is 705 lb, right at the bottom of the CME's 700-899 lb window, so nearly all
    Mexican cattle cross BELOW index weight and reach it only after months on
    US feed. That lag is why an import cut shows up in the index later, not now.
    """
    # Same exclusion-clause split as classify_commodity, for the same reason.
    d, _, _x = (descr or "").upper().partition("OTHER THAN")
    if "LESS THAN 90" in d:
        return "<90 kg (<198 lb)"
    if "90 KG OR MORE" in d and "LESS THAN 200" in d:
        return "90-200 kg (198-441 lb)"
    if "200 KG OR MORE" in d and "LESS THAN 320" in d:
        return "200-320 kg (441-705 lb)"
    if "320 KG OR MORE" in d:
        return "320+ kg (705+ lb)"
    return "unspecified"


def _periods(start: str, end: str):
    """Inclusive YYYY-MM range, as a list of YYYY-MM strings."""
    sy, sm = (int(x) for x in start.split("-"))
    ey, em = (int(x) for x in end.split("-"))
    out = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def ingest(conn, start: str, end: str, ports=True, verbose=True):
    """
    Pull and store monthly Mexican live-cattle imports for a YYYY-MM range.

    One request per month per level, because the timeseries endpoints will
    accept a range but silently cap large ones -- asking month by month is slow
    and boring and always returns everything.
    """
    n = 0
    bad_units = set()
    for period in _periods(start, end):
        nat = _get(NATIONAL_URL, {
            "get": "I_COMMODITY,I_COMMODITY_LDESC,GEN_QY1_MO,GEN_VAL_MO,UNIT_QY1",
            "time": period, "CTY_CODE": MEXICO_CTY,
            "COMM_LVL": "HS10", "I_COMMODITY": HS_LIVE_BOVINE + "*",
        })
        for r in nat:
            unit = (r.get("UNIT_QY1") or "").strip()
            if unit and unit.upper() not in ("NO", "NO.", "NUMBER"):
                bad_units.add(unit)
            db.merge_replace(conn, "census_cattle_imports", COLUMNS,
                             (period, r.get("I_COMMODITY"),
                              r.get("I_COMMODITY_LDESC"), "", None,
                              _num(r.get("GEN_QY1_MO")),
                              _num(r.get("GEN_VAL_MO")), unit or None),
                             ["period", "commodity", "port_code"])
            n += 1

        pn = 0
        if ports:
            prt = _get(PORT_URL, {
                "get": "I_COMMODITY,I_COMMODITY_LDESC,PORT,PORT_NAME,"
                       "GEN_QY1_MO,GEN_VAL_MO,UNIT_QY1",
                "time": period, "CTY_CODE": MEXICO_CTY,
                "COMM_LVL": "HS10", "I_COMMODITY": HS_LIVE_BOVINE + "*",
            })
            for r in prt:
                code = (r.get("PORT") or "").strip()
                if not code:
                    continue      # never let a blank port collide with the national key
                db.merge_replace(conn, "census_cattle_imports", COLUMNS,
                                 (period, r.get("I_COMMODITY"),
                                  r.get("I_COMMODITY_LDESC"), code,
                                  r.get("PORT_NAME"),
                                  _num(r.get("GEN_QY1_MO")),
                                  _num(r.get("GEN_VAL_MO")),
                                  (r.get("UNIT_QY1") or "").strip() or None),
                                 ["period", "commodity", "port_code"])
                n += 1
                pn += 1
        if verbose:
            tot = sum(_num(r.get("GEN_QY1_MO")) or 0 for r in nat)
            print(f"  {period}  {len(nat):>2} codes  {tot:>9,} head  "
                  f"{pn:>3} port rows")
    conn.commit()
    if bad_units and verbose:
        # Loud rather than silent: a non-head unit means every stored number in
        # this pull means something other than head.
        print(f"\n  [!] unexpected UNIT_QY1 values: {sorted(bad_units)} "
              f"-- 'head' may be the wrong label")
    return n


# ---------------------------------------------------------------- read side ---

def monthly_head(conn, kinds=("feeder",)):
    """
    [(period, head)] national totals for the given classification buckets,
    plus the data-through period so the caller can label the lag.
    """
    rows = conn.cursor().execute(
        "SELECT period, commodity, descr, head FROM census_cattle_imports "
        "WHERE port_code = '' ORDER BY period").fetchall()
    agg = {}
    for period, commodity, descr, head in rows:
        if kinds and classify_commodity(commodity, descr) not in kinds:
            continue
        agg[period] = agg.get(period, 0) + (head or 0)
    series = sorted(agg.items())
    return series, (series[-1][0] if series else None)


def yoy(conn, kinds=("feeder",)):
    """
    Year-over-year change per month, and the same-months-YTD comparison.

    YTD compares only months present in BOTH years, so a partial current year
    is never measured against a full prior year -- the same trap that made the
    2026 index YTD read wrong before the weekday alignment went in.
    """
    series, through = monthly_head(conn, kinds)
    by = {}
    for period, head in series:
        y, m = period.split("-")
        by.setdefault(int(y), {})[int(m)] = head
    out = {}
    for y in sorted(by):
        prev = by.get(y - 1, {})
        shared = sorted(set(by[y]) & set(prev))
        cur_ytd = sum(by[y][m] for m in shared)
        prv_ytd = sum(prev[m] for m in shared)
        out[y] = {
            "months": sorted(by[y]),
            "total": sum(by[y].values()),
            "shared_months": shared,
            "ytd": cur_ytd,
            "prior_ytd": prv_ytd,
            "pct": (cur_ytd / prv_ytd - 1) * 100 if prv_ytd else None,
        }
    return out, through


def by_port(conn, period=None, kinds=("feeder",)):
    """[(port_name, head)] for one period, or the latest stored one."""
    cur = conn.cursor()
    if period is None:
        r = cur.execute("SELECT MAX(period) FROM census_cattle_imports "
                        "WHERE port_code <> ''").fetchone()
        period = r[0] if r else None
    if not period:
        return [], None
    rows = cur.execute(
        "SELECT port_name, commodity, descr, head FROM census_cattle_imports "
        f"WHERE port_code <> '' AND period = {db.placeholders(1)}",
        (period,)).fetchall()
    agg = {}
    for name, commodity, descr, head in rows:
        if kinds and classify_commodity(commodity, descr) not in kinds:
            continue
        agg[name] = agg.get(name, 0) + (head or 0)
    return sorted(agg.items(), key=lambda t: -t[1]), period


def commodity_breakdown(conn, period=None):
    """[(commodity, descr, kind, head)] for one period -- the audit view."""
    cur = conn.cursor()
    if period is None:
        r = cur.execute("SELECT MAX(period) FROM census_cattle_imports "
                        "WHERE port_code = ''").fetchone()
        period = r[0] if r else None
    if not period:
        return [], None
    rows = cur.execute(
        "SELECT commodity, descr, head FROM census_cattle_imports "
        f"WHERE port_code = '' AND period = {db.placeholders(1)}",
        (period,)).fetchall()
    out = [(c, d, classify_commodity(c, d), h or 0) for c, d, h in rows]
    return sorted(out, key=lambda t: -t[3]), period


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2019-01", help="YYYY-MM")
    ap.add_argument("--end", default=None, help="YYYY-MM (default: this month)")
    ap.add_argument("--discover", metavar="YYYY-MM",
                    help="just list the HS10 codes carrying volume that month")
    ap.add_argument("--no-ports", action="store_true")
    ap.add_argument("--show", action="store_true", help="read stored data only")
    args = ap.parse_args()

    if args.discover:
        print(f"HS10 codes under {HS_LIVE_BOVINE} with Mexican volume, "
              f"{args.discover}:\n")
        for code, descr, head, unit in discover(args.discover):
            shown = "     none" if head is None else f"{head:>9,}"
            print(f"  {code}  {shown} {str(unit or ''):<5} "
                  f"{classify_commodity(code, descr):<9} {(descr or '')[:84]}")
        return

    conn = db.get_conn()
    init_tables(conn)
    if not args.show:
        end = args.end or date.today().strftime("%Y-%m")
        print(f"Census live cattle from Mexico, {args.start} .. {end}\n")
        n = ingest(conn, args.start, end, ports=not args.no_ports)
        print(f"\nstored {n:,} rows")

    codes, period = commodity_breakdown(conn)
    if period:
        print(f"\n=== HS10 detail, {period} ===")
        for code, descr, kind, head in codes:
            print(f"  {code}  {head:>9,}  {kind:<9} {(descr or '')[:76]}")

    changes, through = yoy(conn)
    print(f"\n=== feeder cattle imports from Mexico (data through {through}) ===")
    for y, d in changes.items():
        pct = "" if d["pct"] is None else f"{d['pct']:+7.1f}%"
        print(f"  {y}  full-year {d['total']:>9,}   "
              f"YTD({len(d['shared_months'])}mo) {d['ytd']:>9,} "
              f"vs {d['prior_ytd']:>9,} {pct}")

    ports, period = by_port(conn)
    if ports:
        print(f"\n=== by port of entry, {period} ===")
        for name, head in ports[:12]:
            print(f"  {str(name)[:40]:<40} {head:>9,}")
    conn.close()


if __name__ == "__main__":
    main()
