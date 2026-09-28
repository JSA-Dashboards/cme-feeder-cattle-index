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

NOT EVERY AMS PRICE IS PER HUNDREDWEIGHT, AND THIS INGEST USED TO ASSUME SO.

AMS sends a price_unit on every auction row. Most are "Per Cwt", but small lots
-- usually three or four head -- are quoted PER ANIMAL, and this filter checked
class, frame, muscle grade, weight break, final_ind, head count, weight and
price while never once looking at the unit. So a per-animal price landed in a
column every reader treats as $/cwt: Dunlap, IA on 2026-08-07 sold 35 head at
442 lb for $2,153.40 A HEAD, which is $487.19/cwt, and it was stored as
$2,153.40/cwt.

The aggregate damage was small because head-weighting dilutes tiny lots (the
400 lb bracket read $532.02 against $530.76 clean). PER BARN it was ruinous:
that one Dunlap row made the barn the chart leader at $1,118.70/cwt, and across
33 bracket/window combinations a contaminated barn reached the top ten in 7 and
led in 5.

This is the "mixed units" trap this project already catalogued for hay, where
Per Bale prices were averaged into a figure labelled $/ton. The lesson there was
"always filter the unit", and it did not get applied here. Now it is:
on_a_cwt_basis() converts the per-animal rows and REJECTS anything it cannot
convert, loudly, rather than storing a number on an unknown basis.

DO NOT REPLACE THAT WITH A PRICE-MAGNITUDE CHECK. The contamination was first
found by looking for avg_price > 1000, and that heuristic is provably
incomplete: Dodge City, KS on 2025-12-17 sold 4 head at 527 lb for $825.00 Per
Unit, which is $156.55/cwt -- wrong in the same way, on the same field, and
invisible to any threshold that a real $/cwt price could also fail. The unit
field is the fact; the price range is a guess.
"""
import argparse
from datetime import date, timedelta

from dotenv import load_dotenv

load_dotenv()

import snowflake_db as db
import update_index as ui

# Below the index band, plus the index band itself -- see the module note.
# 900 added 2026-09-15 for the cash lookup page. The top of this range is
# ABOVE the index's 700-899 band on purpose -- this table is a reference
# for what cattle are bringing, not an index input, and nothing here is
# read by recompute_fci_daily().
CALF_BRACKETS = {400, 450, 500, 550, 600, 650, 700, 750, 800, 850, 900}

# The unit this table stores. Every stored avg_price is $/cwt, by construction --
# there is deliberately NO price_unit column, because a column would invite a
# reader to handle the mixed basis themselves, and the one thing the hay ingest
# proved is that they will not.
#
# Verified against a live MARS payload on 2026-09-18: price_unit is present on
# 100% of auction rows and is spelled lowercase. (Worth stating -- the same API
# capitalises muscle_Grade oddly on the border report's section endpoint, and
# border_reports.py carries a fallback for it.)
PER_CWT_UNIT = "Per Cwt"

# Both spellings, for exactly the reason replacement_reports.py carries both:
# AMS RENAMED ITS PER-ANIMAL UNIT IN 2022, from "Per Head" to "Per Unit".
# Matching only the current label dropped 22,709 rows there -- every 2020 and
# 2021 report -- and did it silently. calf_sales holds nothing older than 2025
# today, but --since takes any date, so the old label is honoured here too.
PER_HEAD_UNITS = {"Per Unit", "Per Head"}

# NOT folded into PER_HEAD_UNITS, and not convertible. A "Per Family" price is a
# cow AND her calf, so dividing it by the cow's weight produces a number that
# means nothing. It has never appeared on a Steers row and should not, but the
# point of naming it is that the rejection below is a decision, not an oversight.
PER_PAIR_UNITS = {"Per Family"}

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

    The price UNIT is deliberately not checked here, so this stays a true mirror
    of the index filter. on_a_cwt_basis() is the next stage and does that job;
    ingest() runs the two in order.
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


def on_a_cwt_basis(rows):
    """
    Put every price on a hundredweight basis, or refuse to store the row.

    Returns (kept, rejected):
      kept      rows whose avg_price is $/cwt. A converted row is a COPY, with
                avg_price replaced and the original preserved as raw_avg_price
                so the caller can see what it superseded. Nothing is mutated in
                place -- the caller still holds AMS's untouched payload.
      rejected  [(reason, row)] for rows this cannot honestly convert. They are
                DROPPED, not stored: a price on an unknown basis is worse than
                an absent one, because it looks exactly like a real number.

    A per-animal price divides by the lot's own average weight, which AMS
    reports on the same row:  $/cwt = price / (avg_weight / 100).

    Rounded to the cent because AMS quotes to the cent, and because avg_price is
    part of the primary key -- a value carried to 14 decimal places would make
    the key depend on float formatting.
    """
    kept, rejected = [], []
    for r in rows:
        unit = (r.get("price_unit") or "").strip()
        if unit == PER_CWT_UNIT:
            kept.append(r)
            continue
        if unit not in PER_HEAD_UNITS:
            # Per Family, an empty unit, or any label AMS invents later.
            rejected.append((f"price_unit={unit or '(missing)'}", r))
            continue
        # Reachable when this is called on unfiltered rows, which is allowed --
        # qualifying_calf_rows() already requires a truthy avg_weight.
        weight = float(r.get("avg_weight") or 0)
        if weight <= 0:
            rejected.append(("per-animal price with no weight", r))
            continue
        out = dict(r)
        out["raw_avg_price"] = float(r["avg_price"])
        out["avg_price"] = round(float(r["avg_price"]) / (weight / 100.0), 2)
        kept.append(out)
    return kept, rejected


def ingest(conn, since: date, until: date, verbose=True):
    """
    Pull auction rows across the wider weight band into calf_sales.

    Mirrors update_index.run_update()'s loop deliberately -- same roster, same
    fetch, same detect_final_sale_day() correction, same weekend shift. A calf
    price that bucketed onto a different date from the feeder price would make
    every backgrounding margin computed across the two quietly wrong, and the
    El Reno episode already showed how expensive a one-day bucketing difference
    is on this data.

    Prices are normalised to $/cwt on the way in by on_a_cwt_basis(), and rows
    that cannot be normalised are dropped with a loud line. See the module
    docstring for the per-animal lots this ingest used to store as $/cwt.
    """
    import json

    auth = ui.get_auth()
    roster = json.loads(ui.ROSTER_PATH.read_text(encoding="utf-8"))
    since_str, until_str = ui.mdY(since), ui.mdY(until)

    ph = db.placeholders(1)

    n = 0
    by_bracket = {}
    converted = 0
    superseded = 0
    rejected_all = []
    for loc in roster:
        slug_id = loc["slug_id"]
        try:
            rows = ui.fetch_slug(slug_id, since_str, until_str, auth)
        except Exception as e:
            if verbose:
                print(f"  [skip] slug {slug_id} ({loc['title']}): {e}")
            continue
        qrows, rejected = on_a_cwt_basis(qualifying_calf_rows(rows))
        rejected_all.extend((loc, reason, r) for reason, r in rejected)
        for r in qrows:
            m, d, y = r["report_date"].split("/")
            sale_date = ui.detect_final_sale_day(
                date(int(y), int(m), int(d)), r.get("report_narrative"))
            bucket = ui.shift_weekend_to_monday(sale_date)
            wl = r["weight_break_low"]
            by_bracket[wl] = by_bracket.get(wl, 0) + 1
            raw = r.get("raw_avg_price")
            if raw is not None and raw != r["avg_price"]:
                converted += 1
                # avg_price is PART OF THE PRIMARY KEY, so a converted row is a
                # different key from the per-animal row an earlier run stored.
                # Without this, re-ingesting would leave the bad row sitting
                # beside the good one forever and double the lot's head count.
                # Targeting the full key plus the raw price makes it exact: the
                # only row it can remove is the one this insert supersedes.
                superseded += conn.cursor().execute(
                    f"DELETE FROM calf_sales WHERE report_date = {ph} "
                    f"AND slug_id = {ph} AND weight_low = {ph} "
                    f"AND muscle_grade = {ph} AND head_count = {ph} "
                    f"AND avg_price = {ph}",
                    (bucket.isoformat(), slug_id, wl, r["muscle_grade"],
                     r["head_count"], raw)).rowcount or 0
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

    # DELIBERATELY NOT GATED ON verbose. update_imports.py calls this with
    # verbose=False, which asks for less per-barn chatter -- it does not ask to
    # be kept in the dark about rows thrown away or history rewritten. A drop
    # nobody is told about is the exact shape of every trap in this repo's
    # docstrings: no error, no warning, just a quietly wrong number.
    if rejected_all:
        reasons = sorted({reason for _, reason, _ in rejected_all})
        print(f"  [!] dropped {len(rejected_all):,} calf row(s) that could not be "
              f"put on a $/cwt basis: {reasons}")
        for loc, reason, r in rejected_all[:5]:
            print(f"      {r.get('report_date')} "
                  f"{loc['city'] or loc['title']}, {loc['state']} "
                  f"{r.get('weight_break_low')}lb {r.get('head_count')}hd "
                  f"${r.get('avg_price')} [{reason}]")
    if superseded:
        print(f"  [!] removed {superseded:,} stored row(s) holding a per-animal "
              f"price in the $/cwt column, replaced by the converted value")

    if verbose:
        print(f"\n  stored {n:,} rows ({converted:,} converted to $/cwt from a "
              f"per-animal price)")
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
