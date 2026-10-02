"""
Does a video sale's lots all publish on one day, or dribble in over two?

ONE OBSERVATION MOTIVATED THIS AND IT IS NOT A RULE. On 2026-10-01 Superior sold
six qualifying lots; AMS published four of them that day and held two until the
morning of 10-02 -- 217 head, including a 155-head block at $299.92 against a
$336.33 day. Our 07:56 run on 10-02 had the four, both competing desks published
later and had all six, and the morning call went out 31 cents high.

Apache's 09-23 and 09-30 sales both published complete on their sale day, so the
split is not universal. Two cases is not a pattern. The next Superior sale is
2026-10-15 (they run every 14 days, Thursday) and this script exists so that
whoever looks on 10-16 gets an answer in one command instead of reconstructing
it, and so the question is not quietly dropped.

WHAT THIS IS NOT. It is not a gate. A publication-date gate was tried on
2026-09-08 and reverted on 2026-09-09 -- see the long note in update_index.py,
which establishes that CME attributes a video sale to its SALE date and that
gating fits CME's provisional first print rather than its final value. Nothing
here should become a filter on what enters the index.

    python video_split.py              # every video sale on record
    python video_split.py --date 2026-10-15

Reads the local database read-only. published_date is populated on video rows
only, and only since the column was added, so history before that is blank
rather than "did not split".
"""
import argparse
import collections
import pathlib
import sqlite3

DB = pathlib.Path(__file__).resolve().parent / "data" / "mars_history.db"


def video_sales(conn, only=None):
    """{sale_date: {published_date or None: [(location, head, price)]}}"""
    sql = ("SELECT raw_date, published_date, location, head_count, avg_weight, "
           "avg_price FROM mars_sales WHERE location LIKE '%VIDEO%'")
    args = []
    if only:
        sql += " AND raw_date = ?"
        args.append(only)
    out = collections.defaultdict(lambda: collections.defaultdict(list))
    for raw, pub, loc, head, wt, price in conn.execute(sql + " ORDER BY raw_date", args):
        out[raw][pub].append((loc, head, wt, price))
    return out


def wavg(lots):
    lb = sum(h * w for _, h, w, _ in lots)
    return (sum(h * w * p for _, h, w, p in lots) / lb if lb else 0.0,
            sum(h for _, h, _, _ in lots))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="a single sale date (YYYY-MM-DD)")
    a = ap.parse_args()

    conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    sales = video_sales(conn, a.date)
    if not sales:
        print("no video sales found" + (f" for {a.date}" if a.date else ""))
        return

    for sale in sorted(sales):
        byday = sales[sale]
        dated = {k: v for k, v in byday.items() if k}
        undated = byday.get(None, [])
        price, head = wavg([x for lots in byday.values() for x in lots])
        print(f"\n  sale {sale}   {head:,} head over {sum(len(v) for v in byday.values())} "
              f"lot(s)   ${price:.2f} weighted")

        if not dated:
            print("    no published_date on any lot -- predates the column, "
                  "so this says nothing either way")
            continue

        for pub in sorted(dated):
            p, h = wavg(dated[pub])
            when = "on the sale day" if pub == sale else f"{_lag(sale, pub)} later"
            print(f"    published {pub} ({when}): {h:>5,} head  ${p:>7.2f}")

        if undated:
            p, h = wavg(undated)
            print(f"    no published_date:            {h:>5,} head  ${p:>7.2f}")

        if len(dated) > 1:
            late = max(dated)
            lp, lh = wavg(dated[late])
            print(f"    -> SPLIT. The {lh:,} head that landed {_lag(sale, late)} late "
                  f"averaged ${lp:.2f}, {lp - price:+.2f} against the sale.")
        else:
            print("    -> single publication day.")


def _lag(sale, pub):
    import datetime
    d = (datetime.date.fromisoformat(pub) - datetime.date.fromisoformat(sale)).days
    return "same day" if d == 0 else f"{d} day{'s' if d != 1 else ''}"


if __name__ == "__main__":
    main()
