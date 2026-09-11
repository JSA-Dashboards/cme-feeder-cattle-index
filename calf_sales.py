"""
Lightweight calf and yearling auction prices -- the BUY leg of a backgrounding
crush, and the weights the feeder cattle index deliberately ignores.

WHY THIS IS A SEPARATE TABLE, AND MUST STAY ONE.

recompute_fci_daily() in update_index.py reads mars_sales with NO WHERE CLAUSE:

    SELECT report_date, raw_date, head_count, avg_weight, avg_price,
           published_date, location FROM mars_sales ORDER BY report_date

Every row in that table is treated as index-qualifying. The filtering happens
once, on the way IN, via qualifying_rows() and TARGET_BRACKETS = {700,750,800,
850}. So widening that ingest to keep 400-650 lb calves would silently blend
500 lb calves at $400/cwt straight into the CME feeder cattle index -- no error,
no warning, just a wrong number on the dashboard and in the morning email.

Hence a separate table the index cannot see. Nothing in update_index.py reads
calf_sales, and nothing here writes mars_sales.

WHAT QUALIFIES HERE. Same shape as the index filter -- Steers, Medium and Large
frame, #1 or #1-2 muscle, Final reports only -- but the weight brackets BELOW
the index band, plus the index band itself so a backgrounding sale can be priced
against the same grade of cattle it will become:

    400, 450, 500, 550, 600, 650   the buy side: calves and light yearlings
    700, 750, 800, 850             the sell side, mirroring the index

650 is included and 700 upward is duplicated from mars_sales ON PURPOSE. A
backgrounder buying 550s and selling 800s wants both ends from one consistent
series, and duplicating a few thousand rows is far cheaper than teaching two
tables to join. The duplication is harmless precisely BECAUSE the index reads
only mars_sales.
"""
import argparse
from datetime import date, timedelta

from dotenv import load_dotenv

load_dotenv()

import snowflake_db as db
import update_index as ui

# Below the index band, plus the index band itself -- see the module note.
CALF_BRACKETS = {400, 450, 500, 550, 600, 650, 700, 750, 800, 850}

COLUMNS = ["report_date", "raw_date", "published_date", "slug_id", "location",
           "state", "weight_low", "weight_high", "muscle_grade", "head_count",
           "avg_weight", "avg_price"]


def init_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS calf_sales (
            report_date TEXT NOT NULL,
            raw_date TEXT,
            published_date TEXT,
            slug_id INTEGER NOT NULL,
            location TEXT NOT NULL,
            state TEXT,
            weight_low INTEGER NOT NULL,
            weight_high INTEGER,
            muscle_grade TEXT NOT NULL,
            head_count INTEGER,
            avg_weight REAL,
            avg_price REAL,
            PRIMARY KEY (report_date, slug_id, weight_low, muscle_grade,
                         avg_price, head_count)
        )
    """)
    conn.commit()


def qualifying_calf_rows(rows):
    """
    Mirrors update_index.qualifying_rows() exactly except for the weight band.

    Kept as its own function rather than parameterising the original: the index
    filter is load-bearing for the published number, and a shared function with
    a bracket argument is one careless default away from widening the index
    itself. Copying six lines is the cheaper risk.
    """
    out = []
    for r in rows:
        if (r.get("class") == "Steers"
                and r.get("frame") == "Medium and Large"
                and r.get("muscle_grade") in ui.TARGET_GRADES
                and r.get("weight_break_low") in CALF_BRACKETS
                and r.get("final_ind") == "Final"
                and r.get("head_count") and r.get("avg_weight")
                and r.get("avg_price")):
            out.append(r)
    return out


def ingest(conn, since: date, until: date, verbose=True):
    """
    Pull auction rows across the wider weight band into calf_sales.

    Mirrors update_index.run_update()'s loop deliberately -- same roster, same
    fetch, same detect_final_sale_day() correction, same weekend shift. A calf
    price that bucketed onto a different date from the feeder price would make
    every backgrounding margin computed across the two quietly wrong, and the
    El Reno episode already showed how expensive a one-day bucketing difference
    is on this data.
    """
    import json

    auth = ui.get_auth()
    roster = json.loads(ui.ROSTER_PATH.read_text(encoding="utf-8"))
    since_str, until_str = ui.mdY(since), ui.mdY(until)

    n = 0
    by_bracket = {}
    for loc in roster:
        slug_id = loc["slug_id"]
        try:
            rows = ui.fetch_slug(slug_id, since_str, until_str, auth)
        except Exception as e:
            if verbose:
                print(f"  [skip] slug {slug_id} ({loc['title']}): {e}")
            continue
        qrows = qualifying_calf_rows(rows)
        for r in qrows:
            m, d, y = r["report_date"].split("/")
            sale_date = ui.detect_final_sale_day(
                date(int(y), int(m), int(d)), r.get("report_narrative"))
            bucket = ui.shift_weekend_to_monday(sale_date)
            wl = r["weight_break_low"]
            by_bracket[wl] = by_bracket.get(wl, 0) + 1
            db.merge_ignore(
                conn, "calf_sales", COLUMNS,
                (bucket.isoformat(), sale_date.isoformat(), None, slug_id,
                 loc["city"] or loc["title"], loc["state"],
                 wl, r.get("weight_break_high"), r["muscle_grade"],
                 r["head_count"], r["avg_weight"], r["avg_price"]),
                ["report_date", "slug_id", "weight_low", "muscle_grade",
                 "avg_price", "head_count"])
            n += 1
        if verbose and qrows:
            print(f"  {loc['state']:>2} {loc['city'] or loc['title']:<28} +{len(qrows)} rows")
    conn.commit()
    if verbose:
        print(f"\n  stored {n:,} rows")
        for wl in sorted(by_bracket):
            band = "index band" if wl >= 700 else "BUY SIDE"
            print(f"    {wl}-{wl + 49} lb  {by_bracket[wl]:>6,}  {band}")
    return n


def price_series(conn, weight_low: int, days: int = 365):
    """[(date, head-weighted $/cwt, head)] for one bracket."""
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = conn.cursor().execute(
        "SELECT report_date, SUM(head_count), "
        "       SUM(head_count * avg_price) / NULLIF(SUM(head_count), 0) "
        f"FROM calf_sales WHERE weight_low = {int(weight_low)} "
        f"AND report_date >= '{since}' "
        "GROUP BY report_date ORDER BY report_date").fetchall()
    return [(str(db.iso(d)), float(p), int(h)) for d, h, p in rows if p]


def latest_by_bracket(conn, days: int = 14):
    """
    {weight_low: {"price", "head", "date"}} -- the most recent head-weighted
    average per bracket, over a trailing window.

    A window rather than the single latest date: any one sale day covers only
    the barns that sold that day, so a one-day read swings on which auctions
    happened to run. Two weeks smooths that without going stale.
    """
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = conn.cursor().execute(
        "SELECT weight_low, SUM(head_count), "
        "       SUM(head_count * avg_price) / NULLIF(SUM(head_count), 0), "
        "       MAX(report_date) "
        f"FROM calf_sales WHERE report_date >= '{since}' "
        "GROUP BY weight_low ORDER BY weight_low").fetchall()
    return {int(w): {"price": float(p), "head": int(h), "date": str(db.iso(d))}
            for w, h, p, d in rows if p}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None, help="ISO date (default: 120 days back)")
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()

    conn = db.get_conn()
    init_tables(conn)
    if not args.show:
        since = (date.fromisoformat(args.since) if args.since
                 else date.today() - timedelta(days=120))
        print(f"Fetching calf/feeder auction rows, {since} .. today\n")
        ingest(conn, since, date.today())

    print("\n=== latest head-weighted average by bracket (trailing 14 days) ===")
    for wl, d in latest_by_bracket(conn).items():
        band = "index band" if wl >= 700 else "BUY SIDE "
        print(f"  {wl}-{wl + 49} lb  {band}  ${d['price']:>7.2f}/cwt  "
              f"{d['head']:>7,} head  through {d['date']}")

    # The index must be untouched by all of this.
    n_mars = conn.cursor().execute("SELECT COUNT(*) FROM mars_sales").fetchone()[0]
    n_calf = conn.cursor().execute("SELECT COUNT(*) FROM calf_sales").fetchone()[0]
    print(f"\n  mars_sales (feeds the index): {n_mars:,} rows")
    print(f"  calf_sales (does not):        {n_calf:,} rows")
    conn.close()


if __name__ == "__main__":
    main()
