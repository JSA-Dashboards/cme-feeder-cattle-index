"""
AMS replacement- and slaughter-cattle auction reports: the weekly, market-priced
read on whether producers are expanding or liquidating the herd.

WHY THIS SOURCE. The definitive herd numbers -- beef cows and beef replacement
heifers -- are NASS January 1 inventory, published once a year. For ten months
of the year a herd-rebuilding view runs on proxies. These reports are weekly,
already covered by the MARS key this repo holds, and priced by people actually
buying and selling breeding females, so they lead the inventory count rather
than confirming it.

WHAT THE REPORTS CARRY, verified 2026-09-10 across 12 active slugs (2,514 rows,
Jul-Sep 2026):

  commodity         Replacement Cattle (1,291), Slaughter Cattle (1,138),
                    Feeder Cattle (85 -- ignored here, the FCI covers it)
  class             Bred Cows 650, Cow-Calf Pairs 265, Stock Cows 176,
                    Bred Heifers 136, Heifer Pairs 18, Open Heifers 11
                    (replacement side); Cows 864, Bulls 318, Dairy Cows 5
                    (slaughter side)
  age               Young (2-4), Middle Aged (5-8), Aged (>8), (<2 yrs)
  pregnancy_stage   Open, 1st (1-3mo), 2nd (4-6mo), 3rd (7-9mo), All Stages
  price_unit        Per Cwt (1,397) and Per Unit (1,117)

PRICE UNITS ARE NOT INTERCHANGEABLE and mixing them is the easiest way to
produce nonsense here. Bred females and pairs trade PER HEAD ("Per Unit");
slaughter cows and bulls trade PER CWT. Every aggregation must filter on
price_unit, which is why it is stored as a column rather than normalised away.

IDEMPOTENCY IS DELETE-THEN-INSERT per (slug_id, report_date), not an upsert.
The natural key is not unique: slaughter cows carry about ten rows per report
under the same class, age and price_unit, separated only by weight and yield
tiers. Measured on five slugs over six weeks, 71 of 895 rows collided on a
nine-column natural key -- an upsert would have silently kept one row in ten.
"""
import argparse
from datetime import date, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

import snowflake_db as db

MARS_BASE = "https://marsapi.ams.usda.gov/services/v1.2"

# Verified active 2026-09-10 by scanning the MARS catalogue for titles matching
# replacement / bred / pairs / breeding, then filtering to those still
# publishing. Slaughter/Replacement combined reports carry both sides, which is
# what makes the retention-incentive ratio computable from one fetch.
REPLACEMENT_SLUGS = {
    1797: "Joplin Regional Stockyards Slaughter/Replacement - Carthage, MO",
    1651: "Ozarks Regional Stockyards Slaughter/Replacement - West Plains, MO",
    1825: "OKC West Livestock Slaughter/Replacement - El Reno, OK",
    1824: "Woodward Livestock Slaughter/Replacement - Woodward, OK",
    1823: "Oklahoma National Stockyards Slaughter/Replacement - OKC, OK",
    1788: "Springfield Livestock Slaughter/Replacement - Springfield, MO",
    1843: "Southern Oklahoma Livestock Slaughter/Replacement - Ada, OK",
    1798: "Joplin Regional Stockyards Replacement Special - Carthage, MO",
    3648: "Tina Livestock Market Replacement Special - Tina, MO",
    1893: "Farmers and Ranchers Replacement - Salina, KS",
    1816: "F and T Livestock Feeder/Replacement - Palmyra, MO",
    2257: "Public Auction Yards Replacement Special - Billings, MT",
}

# Feeder Cattle rows in these reports duplicate ground the FCI already covers,
# and at far worse coverage. Both other commodities are kept: the replacement
# side prices breeding females, the slaughter side prices their salvage value,
# and the RATIO between them is the retention incentive.
KEEP_COMMODITIES = {"Replacement Cattle", "Slaughter Cattle"}

# Classes that represent a breeding female changing hands, i.e. somebody paying
# to expand rather than to feed.
BREEDING_CLASSES = {"Bred Cows", "Bred Heifers", "Cow-Calf Pairs",
                    "Heifer Pairs", "Stock Cows", "Open Heifers"}

# AMS RENAMED ITS PRICE UNITS IN 2022 and both labels have to be honoured. Rows
# priced per animal were "Per Head" in 2020-2021 and are "Per Unit" from 2022;
# pairs were "Per Family" and are now also "Per Unit". Filtering on the current
# label alone silently dropped 22,709 rows -- every 2020 and 2021 report, i.e.
# the first two years of the only history this source has. Same shape as the
# uppercase-TOTALS and str-only-date assumptions elsewhere in this repo: a
# vocabulary that holds for recent data and fails quietly on history.
PER_HEAD_UNITS = {"Per Unit", "Per Head"}

# Deliberately NOT folded into PER_HEAD_UNITS. A "Per Family" price is a cow
# AND her calf, so it is not comparable to a single bred female and must not be
# averaged alongside one.
PER_PAIR_UNITS = {"Per Family"}

COLUMNS = [
    "report_date", "published_date", "slug_id", "market_name", "city", "state",
    "commodity", "class_desc", "age", "pregnancy_stage", "frame",
    "muscle_grade", "price_unit", "head_count", "avg_weight", "avg_price",
    "price_min", "price_max", "receipts", "receipts_year_ago",
]


def init_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS replacement_sales (
            report_date TEXT NOT NULL,
            published_date TEXT,
            slug_id INTEGER NOT NULL,
            market_name TEXT,
            city TEXT,
            state TEXT,
            commodity TEXT,
            class_desc TEXT,
            age TEXT,
            pregnancy_stage TEXT,
            frame TEXT,
            muscle_grade TEXT,
            price_unit TEXT,
            head_count INTEGER,
            avg_weight REAL,
            avg_price REAL,
            price_min REAL,
            price_max REAL,
            receipts INTEGER,
            receipts_year_ago INTEGER
        )
    """)
    conn.commit()


def get_auth():
    import os
    return (os.environ["MARS_API_KEY"], "")


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _mdy_to_iso(s):
    if not s:
        return None
    part = str(s).split()[0]
    m, d, y = part.split("/")
    y = int(y)
    return date(y + 2000 if y < 100 else y, int(m), int(d)).isoformat()


def fetch_slug(slug_id, since, until, auth):
    r = requests.get(f"{MARS_BASE}/reports/{slug_id}", auth=auth,
                     params={"q": f"report_begin_date={since}:{until}"},
                     timeout=(5, 60))
    r.raise_for_status()
    return r.json().get("results", [])


def ingest(conn, since: date, until: date, verbose=True):
    """
    Fetch and store every configured report in the window.

    Deletes each (slug_id, report_date) before re-inserting it, so a re-run or
    an AMS correction replaces a report cleanly rather than layering duplicates
    on top of it. See the module note on why an upsert cannot be used.
    """
    auth = get_auth()
    s, u = since.strftime("%m/%d/%Y"), until.strftime("%m/%d/%Y")
    ph = db.placeholders(len(COLUMNS))
    n_rows = n_reports = 0

    for slug_id, name in REPLACEMENT_SLUGS.items():
        try:
            rows = fetch_slug(slug_id, s, u, auth)
        except Exception as e:
            if verbose:
                print(f"  [!] {slug_id} {name[:34]}: {type(e).__name__}: {e}")
            continue

        keep = [x for x in rows if x.get("commodity") in KEEP_COMMODITIES
                and x.get("head_count") and x.get("avg_price")]
        by_date = {}
        for x in keep:
            by_date.setdefault(_mdy_to_iso(x.get("report_date")), []).append(x)

        for rd, group in sorted(by_date.items()):
            if not rd:
                continue
            conn.cursor().execute(
                f"DELETE FROM replacement_sales WHERE slug_id = {db.placeholders(1)} "
                f"AND report_date = {db.placeholders(1)}", (slug_id, rd))
            for x in group:
                conn.cursor().execute(
                    f"INSERT INTO replacement_sales ({','.join(COLUMNS)}) VALUES ({ph})",
                    (rd, _mdy_to_iso(x.get("published_date")), slug_id,
                     x.get("market_location_name"), x.get("market_location_city"),
                     x.get("market_location_state"), x.get("commodity"),
                     x.get("class"), x.get("age"), x.get("pregnancy_stage"),
                     x.get("frame"), x.get("muscle_grade"), x.get("price_unit"),
                     int(x["head_count"]), _num(x.get("avg_weight")),
                     _num(x.get("avg_price")), _num(x.get("avg_price_min")),
                     _num(x.get("avg_price_max")),
                     int(x["receipts"]) if x.get("receipts") else None,
                     int(x["receipts_year_ago"]) if x.get("receipts_year_ago") else None))
                n_rows += 1
            n_reports += 1
        if verbose and by_date:
            print(f"  {name[:44]:<44} {len(by_date):>2} report(s), {len(keep):>4} rows")
    conn.commit()
    return n_reports, n_rows


def retention_incentive(conn, since_iso=None):
    """
    Bred-cow value against slaughter-cow salvage, per report date.

    This is the retention decision in one number. A cow is worth either what a
    neighbour will pay for her bred, or what the packer will pay for her by the
    pound. When the first far exceeds the second, keeping her pays and the herd
    grows; as the ratio compresses, selling wins.

    Salvage is converted to a per-head basis (Per Cwt price x weight / 100) so
    the two sides are comparable -- bred females trade Per Unit, slaughter cows
    Per Cwt, and comparing them unconverted is meaningless.
    """
    where = f"WHERE report_date >= {db.placeholders(1)}" if since_iso else ""
    args = (since_iso,) if since_iso else ()
    rows = conn.cursor().execute(
        f"SELECT report_date, commodity, class_desc, price_unit, head_count, "
        f"avg_weight, avg_price FROM replacement_sales {where}", args).fetchall()

    per_date = {}
    for rd, commodity, cls, unit, head, wt, price in rows:
        iso = str(db.iso(rd))
        d = per_date.setdefault(iso, {"bred_head": 0, "bred_dollars": 0.0,
                                      "salv_head": 0, "salv_dollars": 0.0})
        if (commodity == "Replacement Cattle" and cls in ("Bred Cows", "Bred Heifers")
                and unit in PER_HEAD_UNITS):
            d["bred_head"] += head
            d["bred_dollars"] += head * price
        elif (commodity == "Slaughter Cattle" and cls == "Cows"
              and unit == "Per Cwt" and wt):
            d["salv_head"] += head
            d["salv_dollars"] += head * price * wt / 100.0

    out = []
    for iso in sorted(per_date):
        d = per_date[iso]
        if not (d["bred_head"] and d["salv_head"]):
            continue
        bred = d["bred_dollars"] / d["bred_head"]
        salv = d["salv_dollars"] / d["salv_head"]
        out.append({"date": iso, "bred_per_head": bred, "salvage_per_head": salv,
                    "premium": bred - salv, "ratio": bred / salv,
                    "bred_head": d["bred_head"], "salvage_head": d["salv_head"]})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None, help="ISO date (default: 120 days back)")
    ap.add_argument("--until", default=None, help="ISO date (default: today)")
    ap.add_argument("--show", action="store_true", help="print the retention incentive and exit")
    args = ap.parse_args()

    conn = db.get_conn()
    init_tables(conn)

    if not args.show:
        since = (date.fromisoformat(args.since) if args.since
                 else date.today() - timedelta(days=120))
        until = date.fromisoformat(args.until) if args.until else date.today()
        print(f"Fetching {len(REPLACEMENT_SLUGS)} replacement reports, "
              f"{since} .. {until}\n")
        n_reports, n_rows = ingest(conn, since, until)
        print(f"\nstored {n_rows:,} rows across {n_reports} report-dates")

    inc = retention_incentive(conn)
    if inc:
        print(f"\n{'date':<12}{'bred $/hd':>11}{'salvage $/hd':>14}"
              f"{'premium':>10}{'ratio':>8}{'bred hd':>9}")
        for r in inc[-14:]:
            print(f"{r['date']:<12}{r['bred_per_head']:>11,.0f}"
                  f"{r['salvage_per_head']:>14,.0f}{r['premium']:>10,.0f}"
                  f"{r['ratio']:>8.2f}{r['bred_head']:>9,}")
    conn.close()


if __name__ == "__main__":
    main()
