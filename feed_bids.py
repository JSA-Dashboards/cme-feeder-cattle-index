"""
Distillers grains and hay -- the non-corn half of a cattle ration.

WHY. The Fed Cattle Crush builds cost of gain from a delivered corn price backed
by hundreds of live quotes, then adds "Other feed ($/ton)" as a number the user
types. At an 80% ration that leaves a fifth of the feed bill unpriced, and on
the backgrounding side it is far worse: a grower ration is forage-led, so the
typed number IS most of the cost. Iowa State's 2026 budgets put backgrounding
cost of gain at $109-111/cwt against $107-111 for finishing, and what makes
backgrounding gain cheap when it is cheap is feed at $114-134 a ton of dry
matter instead of $217 -- not conversion, and not corn. Pricing that ton is the
point of this module.

TWO SOURCES, VERY DIFFERENT QUALITY.

  DISTILLERS (AMS_3618, National Weekly Grain Co-Products) is the clean one.
  ONE call returns wet and dry for 11 states, $ Per Ton, weekly. Zero null and
  zero zero-priced rows measured over ten weeks. Strictly less work than
  corn_bids.py, which makes eleven calls.

  HAY (15 per-state Direct Hay reports) is not clean, and carries two traps that
  a straight copy of corn_bids.py would inherit silently. Both are handled
  below and both are worth reading before changing anything here.

TRAP 1 -- ZERO IS NOT A PRICE. 35% of Direct Hay rows carry wtd_Avg_Price == 0,
the literal integer, not null. They concentrate in sale_Type 'Ask' and 'Offer'
rows -- an asking price with no trade behind it -- and Texas and Missouri are
100% zero. AVG() over those craters the state average toward nothing, silently.
The rows are kept, because knowing an ask existed is worth something, but the
price is stored as NULL so no average can ever include it.

TRAP 2 -- CLASS AVERAGING LIES. Iowa's 'grass' Trade average is $251.11/ton,
ABOVE Iowa alfalfa at $184.33, which is agronomically backwards. The cause is
five near-duplicate rows of Premium 'Alfalfa/Grass Mix' in 3x3 mediums at
$270-280 -- dairy hay, priced for a dairy. The one genuine feed-quality grass
row is $116/ton. So quality and package are part of the key and are kept on
every row, and feed_quality_hay() filters to what a feedyard would actually buy
rather than averaging a class.

DRY MATTER, NOT AS-FED. Wet distillers at $51/ton looks like a third the price
of dry at $169 until you notice it is 65-70% water. On a dry-matter basis they
are $157 and $188 -- close to parity, and close to grass hay at $157 DM. Any
ration arithmetic that mixes wet feeds with dry ones on an as-fed basis is
wrong by a factor of three. dm_price() exists so that cannot happen quietly.

Nothing here touches mars_sales or fci_daily. Feed cannot reach the index.
"""
import argparse
from datetime import date, timedelta
from urllib.parse import quote

import requests
from dotenv import load_dotenv

load_dotenv()

import snowflake_db as db

MARS_BASE = "https://marsapi.ams.usda.gov/services/v1.2"

# AMS_3618 names states in full inside trade_loc; everything else in this repo
# keys on the postal abbreviation.
DG_STATES = {
    "Iowa": "IA", "Kansas": "KS", "Missouri": "MO", "Nebraska": "NE",
    "South Dakota": "SD", "Minnesota": "MN", "Illinois": "IL",
    "Wisconsin": "WI", "Ohio": "OH", "Michigan": "MI", "Indiana": "IN",
}

# Direct Hay, one report per state. Slug ids confirmed against a live catalog
# listing on 2026-09-14. Section is "Report Details" -- PLURAL -- where the
# grain and co-product reports use "Report Detail". Not a typo: the section
# name genuinely differs between report families, and passing the wrong one
# returns the header with no rows and no error.
HAY_STATES = {
    "TX": 2707, "MT": 2769, "IA": 2807, "KS": 2885, "CA": 2904, "CO": 2905,
    "MO": 2929, "NE": 2935, "NM": 2939, "ID": 3056, "OK": 3095, "SD": 3183,
    "WY": 3236, "UT": 3731, "AZ": 3784,
}

# The cattle-feeding states corn_cost.py prices corn for. Minnesota and North
# Dakota have no Direct Hay report at all, so hay coverage is 9 of 11.
FEEDING_STATES = ["TX", "KS", "NE", "CO", "OK", "IA", "SD", "MO", "MN", "ND", "WY"]

# Approximate dry matter fraction by feed. Wet distillers is the one that
# matters -- quoted as-fed at 65-70% moisture, so a ton is roughly a third feed.
DM_FRACTION = {
    "Wet 65-70%": 0.325,
    "Dried 10%": 0.90,
    "hay": 0.87,
}

DG_COLUMNS = ["report_date", "published_date", "state", "variety",
              "price_ton", "price_unit", "corn_equiv_bu"]

HAY_COLUMNS = ["report_date", "published_date", "state", "slug_id", "hay_class",
               "quality", "package", "region", "sale_type", "hay_use",
               "crop_age", "freight", "hay_desc", "price_min", "price_max",
               "avg_price", "quantity", "price_unit", "conventional"]


def init_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS distillers_bids (
            report_date TEXT NOT NULL,
            published_date TEXT,
            state TEXT NOT NULL,
            variety TEXT NOT NULL,
            price_ton REAL,
            price_unit TEXT,
            corn_equiv_bu REAL,
            PRIMARY KEY (report_date, state, variety)
        )
    """)
    # The hay key is wide ON PURPOSE. corn_bids lost 76% of its rows to a key
    # missing trade_loc -- 1,912 ingested became 448 stored -- and the only
    # thing that exposed it was a row-count mismatch. A state publishes many
    # hay rows a day that differ only by quality or package, so every field
    # that legitimately varies within a report is part of the key.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hay_bids (
            report_date TEXT NOT NULL,
            published_date TEXT,
            state TEXT NOT NULL,
            slug_id INTEGER NOT NULL,
            hay_class TEXT NOT NULL,
            quality TEXT NOT NULL,
            package TEXT NOT NULL,
            region TEXT NOT NULL,
            sale_type TEXT NOT NULL,
            hay_use TEXT NOT NULL,
            crop_age TEXT NOT NULL,
            freight TEXT NOT NULL,
            hay_desc TEXT NOT NULL,
            price_min REAL,
            price_max REAL,
            avg_price REAL,
            quantity REAL,
            price_unit TEXT NOT NULL,
            conventional TEXT NOT NULL,
            PRIMARY KEY (report_date, state, hay_class, quality, package,
                         region, sale_type, hay_use, crop_age, freight,
                         hay_desc, price_unit, conventional)
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


def _fetch(slug_id, section, since, until, timeout=180):
    r = requests.get(f"{MARS_BASE}/reports/{slug_id}/{quote(section)}",
                     auth=get_auth(),
                     params={"q": f"report_begin_date="
                                  f"{since.strftime('%m/%d/%Y')}:"
                                  f"{until.strftime('%m/%d/%Y')}"},
                     timeout=(5, timeout))
    if r.status_code in (204, 404):
        return []
    r.raise_for_status()
    return r.json().get("results", [])


def ingest_distillers(conn, since: date, until: date, verbose=True):
    """Wet and dry distillers, all 11 states, in a single call."""
    try:
        rows = _fetch(3618, "Report Detail", since, until)
    except Exception as e:
        if verbose:
            print(f"  [!] distillers: {type(e).__name__}: {e}")
        return 0

    n = 0
    for x in rows:
        if "distillers grain" not in str(x.get("commodity", "")).lower():
            continue          # skips Distillers Corn Oil, priced in cents/lb
        st = DG_STATES.get(str(x.get("trade_loc") or "").strip())
        iso = _mdy_to_iso(x.get("report_date"))
        variety = (x.get("variety") or "").strip()
        price = _num(x.get("price"))
        if not (st and iso and variety) or price is None:
            continue
        db.merge_replace(
            conn, "distillers_bids", DG_COLUMNS,
            (iso, _mdy_to_iso(x.get("published_date")), st, variety, price,
             (x.get("price_unit") or "").strip() or None, _num(x.get("value"))),
            ["report_date", "state", "variety"])
        n += 1
    conn.commit()
    if verbose:
        print(f"  distillers  {n:>6} rows across {len(DG_STATES)} states")
    return n


def ingest_hay(conn, since: date, until: date, verbose=True):
    """
    Per-state Direct Hay. Returns rows STORED, and prints rows seen alongside,
    because a gap between the two is what exposed the corn_bids key bug.
    """
    seen = zeroed = 0
    keys = set()
    for st, slug_id in HAY_STATES.items():
        try:
            rows = _fetch(slug_id, "Report Details", since, until)
        except Exception as e:
            if verbose:
                print(f"  [!] {st} hay: {type(e).__name__}: {e}")
            continue
        kept = 0
        for x in rows:
            if "hay" not in str(x.get("commodity", "")).lower():
                continue      # the same reports carry straw and a little silage
            # DIRECT HAY HAS NO report_date FIELD. It carries report_begin_date
            # and report_end_date only -- these are period reports, roughly
            # weekly, not daily prints. Asking for report_date returns None and
            # silently skips every row, which is exactly what the first run of
            # this ingest did: 0 rows seen, no error. Same family of trap as the
            # section name being "Report Details" here and "Report Detail" on
            # the grain reports -- one report family's schema does not carry
            # over to another.
            #
            # The END date is used as the sale date: it is the last day the
            # quoted trades could have happened, which is the right anchor for
            # a trailing window.
            iso = _mdy_to_iso(x.get("report_end_date")
                              or x.get("report_begin_date"))
            if not iso:
                continue
            seen += 1
            avg = _num(x.get("wtd_Avg_Price"))
            # ZERO IS NOT A PRICE. It marks an Ask or Offer with no trade
            # behind it. Keep the row, drop the number.
            if avg == 0:
                avg = None
                zeroed += 1
            db.merge_replace(
                conn, "hay_bids", HAY_COLUMNS,
                (iso, _mdy_to_iso(x.get("published_date")), st, slug_id,
                 (x.get("class") or "").strip() or "-",
                 (x.get("quality") or "").strip() or "-",
                 (x.get("package") or "").strip() or "-",
                 (x.get("region") or "").strip() or "-",
                 (x.get("sale_Type") or "").strip() or "-",
                 (x.get("use") or "").strip() or "-",
                 (x.get("crop_Age") or "").strip() or "-",
                 (x.get("freight") or "").strip() or "-",
                 (x.get("desc") or "").strip() or "-",
                 _num(x.get("price_Min")), _num(x.get("price_Max")), avg,
                 _num(x.get("quantity")),
                 (x.get("price_Unit") or "").strip() or "-",
                 (x.get("conventional") or "").strip() or "-"),
                ["report_date", "state", "hay_class", "quality", "package",
                 "region", "sale_type", "hay_use", "crop_age", "freight",
                 "hay_desc", "price_unit", "conventional"])
            kept += 1
            keys.add((iso, st, x.get("class"), x.get("quality"), x.get("package"),
                      x.get("region"), x.get("sale_Type"), x.get("use"),
                      x.get("crop_Age"), x.get("freight"), x.get("desc"),
                      x.get("price_Unit"), x.get("conventional")))
        if verbose and kept:
            print(f"  {st} hay {kept:>5} rows")
    stored = len(keys)
    conn.commit()
    if verbose:
        print(f"  hay         {seen:>6} seen, {stored:>6} stored, "
              f"{zeroed} price-zero rows kept with a NULL price")
        if stored != seen:
            print(f"  [!] {seen - stored} rows COLLAPSED -- the primary key is "
                  f"missing a field that varies. This is how corn_bids lost "
                  f"76% of its rows.")
    return stored


def dm_price(price_ton, feed_key):
    """
    As-fed $/ton converted to a DRY MATTER $/ton.

    The single most important function here. Wet distillers at $51/ton is not
    cheap feed, it is $157/ton of actual feed plus a ton and a half of water.
    """
    frac = DM_FRACTION.get(feed_key)
    if not frac or price_ton is None:
        return None
    return price_ton / frac


def state_distillers(conn, days: int = 45):
    """
    {state: {variety: {"as_fed", "dm", "n"}}}. A 45-day window because this
    report is WEEKLY -- a 10-day window like corn's would catch one print, or
    none if a week was missed.
    """
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = conn.cursor().execute(
        "SELECT state, variety, AVG(price_ton), COUNT(*) FROM distillers_bids "
        f"WHERE report_date >= '{since}' AND price_ton IS NOT NULL "
        "GROUP BY state, variety").fetchall()
    out = {}
    for st, variety, p, n in rows:
        out.setdefault(str(st), {})[str(variety)] = {
            "as_fed": float(p), "dm": dm_price(float(p), str(variety)),
            "n": int(n)}
    return out


def feed_quality_hay(conn, days: int = 60):
    """
    {state: {"price", "dm", "n", "class"}} -- FEEDER hay, not dairy hay.

    Deliberately not an average over a class. Iowa's 'grass' Trade average is
    $251.11/ton, above its alfalfa at $184.33, because five Premium
    Alfalfa/Grass Mix rows in 3x3 mediums at $270-280 are dairy hay sitting in
    the same bucket. Averaging a class reproduces that error in every state.

    So: traded rows only, priced rows only, and Premium/Supreme excluded --
    those grades exist for dairies and a feedyard does not buy them for a
    growing ration. What is left is Good, Fair and Utility hay in large rounds
    and big squares, which is what actually goes in a bunk.
    """
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = conn.cursor().execute(
        # TONNAGE-WEIGHTED, not a plain average of quoted prices. A 102-ton lot
        # and an 18-ton lot are not equal evidence of what hay costs, and the
        # index this repo reconstructs weights by pounds for the same reason.
        # Rows with no quantity fall back to a weight of 1 so they still count.
        "SELECT state, hay_class, "
        "       SUM(avg_price * COALESCE(quantity, 1)) "
        "         / SUM(COALESCE(quantity, 1)), "
        "       COUNT(*), SUM(COALESCE(quantity, 0)) FROM hay_bids "
        f"WHERE report_date >= '{since}' AND avg_price IS NOT NULL "
        "  AND avg_price > 0 AND sale_type = 'Trade' "
        "  AND price_unit = 'Per Ton' "
        # AMS tags the destination in `use`, which beats inferring it from
        # grade. Feedlot and Farm/Ranch are both cattle feed. Dairy, Stables
        # and Retail are not, and they are not a rounding error -- Colorado
        # reports 105 Stables and 97 Retail rows against 4 Feedlot, so a state
        # average that keeps them is a horse-hay price wearing a feedyard
        # label. Dealer/Mill/Processor is a middleman quote, not a delivered
        # feed cost.
        #
        # Most rows carry no use at all (Kansas 467 of 603), so the grade
        # fallback still does most of the work -- it is what the Iowa
        # grass-over-alfalfa inversion came from.
        "  AND (hay_use IN ('Feedlot', 'Farm/Ranch') "
        "       OR (hay_use = '-' AND quality NOT LIKE '%Premium%' "
        "           AND quality NOT LIKE '%Supreme%')) "
        "GROUP BY state, hay_class").fetchall()
    best = {}
    for st, cls, p, n, tons in rows:
        st, p, n = str(st), float(p), int(n)
        # Prefer the deepest class in each state rather than blending classes,
        # which would average alfalfa against grass and mean neither.
        if st not in best or n > best[st]["n"]:
            best[st] = {"price": p, "dm": dm_price(p, "hay"), "n": n,
                        "tons": float(tons or 0), "class": cls}
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None, help="ISO date (default: 90 days back)")
    ap.add_argument("--show", action="store_true", help="skip the fetch")
    a = ap.parse_args()

    conn = db.get_conn()
    init_tables(conn)
    if not a.show:
        since = (date.fromisoformat(a.since) if a.since
                 else date.today() - timedelta(days=90))
        print(f"Fetching feed prices, {since} .. today\n")
        ingest_distillers(conn, since, date.today())
        ingest_hay(conn, since, date.today())

    print("\n=== distillers, trailing 45 days ===")
    for st, v in sorted(state_distillers(conn).items()):
        parts = [f"{k} ${d['as_fed']:>6.2f}/t as-fed (${d['dm']:>6.2f} DM, n={d['n']})"
                 for k, d in sorted(v.items())]
        print(f"  {st}  " + "   ".join(parts))

    print("\n=== feed-quality hay, trailing 60 days ===")
    for st, d in sorted(feed_quality_hay(conn).items()):
        print(f"  {st}  ${d['price']:>7.2f}/ton as-fed  "
              f"${d['dm']:>7.2f}/ton DM   {d['class'][:20]:<22} "
              f"n={d['n']:<3} {d['tons']:>7,.0f} tons"
              + ("   <- THIN" if d["n"] < 5 else ""))

    # Name the feeding states this cannot price rather than leaving a caller to
    # notice the absence. TX and MO publish Ask and Offer rows ONLY -- every
    # price is a literal zero, 124 and 66 rows with not one trade between them
    # -- and MN and ND have no Direct Hay report at all.
    have = set(feed_quality_hay(conn))
    missing = [x for x in FEEDING_STATES if x not in have]
    if missing:
        print(f"\n  NO usable hay price for: {', '.join(missing)}")
        print("  TX and MO publish Ask/Offer only, never a traded price; "
              "MN and ND have no Direct Hay report.")
    conn.close()


if __name__ == "__main__":
    main()
