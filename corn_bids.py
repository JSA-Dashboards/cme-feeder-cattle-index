"""
Cash corn bids by state -- the feed side of a cost-of-gain build-up.

WHY CASH AND NOT FUTURES. A feeder does not buy the board, they buy corn from
an elevator down the road, and the gap between the two is basis that varies by
several dimes across the feeding states. Pricing cost of gain off Chicago would
be wrong in a direction that changes by geography: at the time of writing,
Nebraska bid 5.31 and Kansas 5.11 on the same day. The whole point of a
cost-of-gain build-up is that a Kansas feeder gets a Kansas number.

STATE COVERAGE is the cattle-feeding states only, not all 27 grain-bid reports
AMS publishes. A Maryland corn bid has no bearing on a feedyard margin and
would just be rows to skip.

ONE BID PER REGION, NOT PER STATE. Each state report is broken into trade
locations -- Nebraska publishes Central, East, Northwest, South, Southeast and
Southwest every day, and on 2026-09-10 they ranged $4.77 to $5.41. trade_loc is
therefore part of the primary key. The first version of this table left it out,
and five of every six Nebraska rows were silently overwritten: 1,912 rows
ingested became 448 stored, and every state average was computed over whichever
region happened to be written last. The row count mismatch is what exposed it.

That regional split is worth having rather than merely tolerating: a Panhandle
feedyard and an eastern Nebraska one do not buy the same corn.

Feeds the Fed Cattle Crush page's corn-built cost of gain. Nothing here touches
mars_sales or fci_daily; corn cannot reach the feeder cattle index.
"""
import argparse
from datetime import date, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

import snowflake_db as db

MARS_BASE = "https://marsapi.ams.usda.gov/services/v1.2"

# The states that actually feed cattle. Slug ids confirmed from a live report
# listing on 2026-09-11.
CORN_STATES = {
    "KS": (2886, "Kansas Daily Grain Bids"),
    "NE": (3225, "Nebraska Daily Elevator Grain Bids"),
    "TX": (2711, "Texas Daily Grain Bids"),
    "OK": (3100, "Oklahoma Daily Grain Bids"),
    "IA": (2850, "Iowa Daily Cash Grain Bids"),
    "CO": (2912, "Colorado Daily Grain Bids"),
    "SD": (3186, "South Dakota Daily Grain Bids"),
    "MO": (2932, "Missouri Daily Grain Bids"),
    "ND": (3878, "North Dakota Daily Grain Bids"),
    "MN": (3049, "Southern Minnesota Daily Grain Bids"),
    "WY": (3239, "Wyoming Daily Grain Bids"),
}

COLUMNS = ["report_date", "published_date", "state", "slug_id", "trade_loc",
           "delivery_point", "grain_class", "price_min", "price_max",
           "avg_price", "price_unit"]


def init_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS corn_bids (
            report_date TEXT NOT NULL,
            published_date TEXT,
            state TEXT NOT NULL,
            slug_id INTEGER NOT NULL,
            trade_loc TEXT NOT NULL,
            delivery_point TEXT NOT NULL,
            grain_class TEXT,
            price_min REAL,
            price_max REAL,
            avg_price REAL,
            price_unit TEXT,
            PRIMARY KEY (report_date, state, trade_loc, delivery_point, grain_class)
        )
    """)
    conn.commit()


def get_auth():
    import os
    return (os.environ["MARS_API_KEY"], "")


def _mdy_to_iso(s):
    if not s:
        return None
    m, d, y = str(s).split()[0].split("/")
    y = int(y)
    return date(y + 2000 if y < 100 else y, int(m), int(d)).isoformat()


def _num(v):
    if v is None:
        return None
    t = str(v).strip().replace(",", "").replace("$", "")
    if not t or t.upper() in ("NA", "N/A", "NULL", "-"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def ingest(conn, since: date, until: date, verbose=True):
    """
    Corn rows only, from each state's "Report Detail" section.

    The section is a PATH segment -- asking for the report without one returns
    the header and no numbers at all, which is how the border reports were once
    mistaken for having no data. Same trap, same fix.
    """
    from urllib.parse import quote
    s, u = since.strftime("%m/%d/%Y"), until.strftime("%m/%d/%Y")
    auth = get_auth()
    n = 0
    for st, (slug_id, title) in CORN_STATES.items():
        try:
            r = requests.get(f"{MARS_BASE}/reports/{slug_id}/{quote('Report Detail')}",
                             auth=auth, params={"q": f"report_begin_date={s}:{u}"},
                             timeout=(5, 120))
            if r.status_code in (204, 404):
                rows = []
            else:
                r.raise_for_status()
                rows = r.json().get("results", [])
        except Exception as e:
            if verbose:
                print(f"  [!] {st}: {type(e).__name__}: {e}")
            continue

        kept = 0
        for x in rows:
            if "corn" not in str(x.get("commodity", "")).lower():
                continue
            iso = _mdy_to_iso(x.get("report_date"))
            dp = (x.get("delivery_point") or x.get("market_location_name") or "").strip()
            if not iso or not dp:
                continue
            lo, hi = _num(x.get("price Min")), _num(x.get("price Max"))
            avg = _num(x.get("avg_price"))
            if avg is None and lo is not None and hi is not None:
                avg = (lo + hi) / 2
            if avg is None:
                continue
            db.merge_replace(
                conn, "corn_bids", COLUMNS,
                (iso, _mdy_to_iso(x.get("published_date")), st, slug_id,
                 (x.get("trade_loc") or "All").strip() or "All", dp,
                 (x.get("class") or "").strip() or None, lo, hi, avg,
                 (x.get("price_unit") or "").strip() or None),
                ["report_date", "state", "trade_loc", "delivery_point",
                 "grain_class"])
            kept += 1
            n += 1
        if verbose:
            print(f"  {st}  {title[:40]:<42}{kept:>6} corn rows")
    conn.commit()
    return n


def state_corn(conn, days: int = 10):
    """
    {state: {"price", "n", "date"}} -- average cash corn bid per state.

    A TRAILING WINDOW, not the latest date. Not every elevator reports every
    day, so a single day's average is whichever subset happened to file, and it
    jumps around for reasons that have nothing to do with the corn market. Ten
    days smooths that while staying current enough to feed a margin.
    """
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = conn.cursor().execute(
        "SELECT state, AVG(avg_price), COUNT(*), MAX(report_date) "
        f"FROM corn_bids WHERE report_date >= '{since}' "
        "GROUP BY state ORDER BY state").fetchall()
    return {str(s): {"price": float(p), "n": int(c), "date": str(db.iso(d))}
            for s, p, c, d in rows if p}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None, help="ISO date (default: 30 days back)")
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()

    conn = db.get_conn()
    init_tables(conn)
    if not args.show:
        since = (date.fromisoformat(args.since) if args.since
                 else date.today() - timedelta(days=30))
        print(f"Fetching cash corn bids, {since} .. today\n")
        n = ingest(conn, since, date.today())
        print(f"\nstored {n:,} rows")

    print("\n=== average cash corn bid by state (trailing 10 days) ===")
    for st, d in sorted(state_corn(conn).items(), key=lambda kv: -kv[1]["price"]):
        print(f"  {st}  ${d['price']:>5.2f}/bu   {d['n']:>4} quotes   through {d['date']}")
    conn.close()


if __name__ == "__main__":
    main()
