"""
What the index is made of: the 7-day window broken out by weight bracket and
muscle grade, and what the grade mix is doing to the price.

Built from OUR mars_sales rather than CME's published brackets, deliberately.
CME's per-bracket rows only exist for dates CME has already printed, so a panel
driven by them would always be a day or more stale -- exactly the date the user
is least interested in. mars_sales carries weight_low and muscle_grade with the
same values CME uses ('1' / '1-2', 700/750/800/850), so the live estimate can be
decomposed the moment it is computed. CME's brackets are used only for the
historical baseline, where being settled is a virtue.

Bucketing is applied the same way recompute_fci_daily() applies it, or the
composition would not add up to the index printed beside it -- the same trap
that had the 7-day window table showing 2,176 head against an index built on
2,129.

THE MIX EFFECT is the reason this panel earns its place. Measured 2026-09-10
over ISO weeks 33-41: the #1-2 share of pounds went from 17.6% (2019-2023) to
27.7% (2026) while #1-2 cattle carried a $14.57 discount, holding the index
about $1.47/cwt below where the old grade composition would have put it. That
is a real drag on the printed level, invisible in the headline price, and about
370x our own estimation error.
"""
from datetime import date, timedelta

import snowflake_db as db
from bucketing import shifted_bucket_date

BRACKETS = [700, 750, 800, 850]
GRADES = ["1", "1-2"]

# Years pooled for the grade-mix baseline. Matches the 5-year volume norm so
# the two panels do not quietly disagree about what "normal" means.
BASELINE_YEARS = (2021, 2025)


def window_composition(conn, index_date_iso, days=7):
    """
    {(weight_low, grade): {head, lbs, dollars, avg_weight, price}} for the
    rolling window ending on index_date_iso, from our own sales.
    """
    start = (date.fromisoformat(index_date_iso) - timedelta(days=days - 1)).isoformat()
    rows = conn.cursor().execute(
        f"SELECT report_date, location, weight_low, muscle_grade, head_count, "
        f"avg_weight, avg_price FROM mars_sales "
        f"WHERE report_date BETWEEN {db.placeholders(1)} AND {db.placeholders(1)}",
        # Widen the fetch by the largest bucketing shift so a sale that moves
        # INTO the window is not missed, then filter on the shifted date below.
        ((date.fromisoformat(start) - timedelta(days=2)).isoformat(),
         (date.fromisoformat(index_date_iso) + timedelta(days=2)).isoformat()),
    ).fetchall()

    cells = {}
    for rd, loc, wl, grade, head, wt, price in rows:
        bucket = shifted_bucket_date(loc, str(db.iso(rd)))
        if not (start <= bucket <= index_date_iso):
            continue
        if wl is None or grade is None or not head or not wt or not price:
            continue
        key = (int(wl), str(grade))
        c = cells.setdefault(key, {"head": 0, "lbs": 0.0, "dollars": 0.0})
        c["head"] += int(head)
        c["lbs"] += head * wt
        c["dollars"] += head * wt * price

    for c in cells.values():
        c["avg_weight"] = c["lbs"] / c["head"] if c["head"] else None
        c["price"] = c["dollars"] / c["lbs"] if c["lbs"] else None
    return cells


def _grade_totals(cells):
    out = {g: {"head": 0, "lbs": 0.0, "dollars": 0.0} for g in GRADES}
    for (_wl, g), c in cells.items():
        if g not in out:
            continue
        out[g]["head"] += c["head"]
        out[g]["lbs"] += c["lbs"]
        out[g]["dollars"] += c["dollars"]
    for g in out:
        out[g]["price"] = (out[g]["dollars"] / out[g]["lbs"]) if out[g]["lbs"] else None
    return out


def baseline_grade_share(conn, index_date_iso, years=BASELINE_YEARS):
    """
    (#1-2 share of POUNDS, n_years) for this ISO week across the baseline years,
    from CME's own published brackets. Pounds, not head, because the index is
    pound-weighted and a head share would misstate the effect.
    """
    wk = date.fromisoformat(index_date_iso).isocalendar()[1]
    try:
        rows = conn.cursor().execute(
            "SELECT report_date, grade, SUM(head_count * avg_weight) "
            "FROM cme_ftp_brackets WHERE avg_weight > 0 "
            "GROUP BY report_date, grade").fetchall()
    except Exception:
        return None, 0

    pooled, seen_years = {g: 0.0 for g in GRADES}, set()
    for rd, g, lbs in rows:
        d = date.fromisoformat(str(db.iso(rd)))
        if years[0] <= d.year <= years[1] and d.isocalendar()[1] == wk:
            if str(g) in pooled:
                pooled[str(g)] += float(lbs or 0)
                seen_years.add(d.year)
    total = sum(pooled.values())
    if not total or len(seen_years) < 2:
        return None, len(seen_years)
    return pooled["1-2"] / total, len(seen_years)


def mix_effect(conn, index_date_iso):
    """
    What the current grade mix is doing to the index price, in $/cwt.

    Holds today's own #1 and #1-2 prices and swaps in the baseline pound share:
    the difference is attributable to composition rather than to either grade
    getting cheaper. Returns None where the baseline or either grade is absent.
    """
    cells = window_composition(conn, index_date_iso)
    if not cells:
        return None
    tot = _grade_totals(cells)
    lbs = sum(t["lbs"] for t in tot.values())
    if not lbs or not all(tot[g]["lbs"] for g in GRADES):
        return None

    share_now = tot["1-2"]["lbs"] / lbs
    share_base, n_years = baseline_grade_share(conn, index_date_iso)
    if share_base is None:
        return None

    p1, p12 = tot["1"]["price"], tot["1-2"]["price"]
    actual = (1 - share_now) * p1 + share_now * p12
    counter = (1 - share_base) * p1 + share_base * p12
    return {
        "share_now": share_now, "share_base": share_base, "baseline_years": n_years,
        "price_1": p1, "price_1_2": p12, "spread": p12 - p1,
        "actual": actual, "counterfactual": counter, "effect": actual - counter,
        "head_1": tot["1"]["head"], "head_1_2": tot["1-2"]["head"],
    }


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    conn = db.get_conn()
    latest = str(db.iso(conn.cursor().execute(
        "SELECT MAX(report_date) FROM fci_daily").fetchone()[0]))
    cells = window_composition(conn, latest)
    print(f"7-day window ending {latest}\n")
    print(f"{'bracket':<10}{'#1 head':>9}{'#1 price':>10}"
          f"{'#1-2 head':>11}{'#1-2 price':>12}{'total':>9}{'share':>8}")
    grand = sum(c["head"] for c in cells.values())
    for wl in BRACKETS:
        a = cells.get((wl, "1"), {})
        b = cells.get((wl, "1-2"), {})
        tot = a.get("head", 0) + b.get("head", 0)
        print(f"{wl}-{wl+49:<5}{a.get('head', 0):>9,}"
              f"{('$%.2f' % a['price']) if a.get('price') else '—':>10}"
              f"{b.get('head', 0):>11,}"
              f"{('$%.2f' % b['price']) if b.get('price') else '—':>12}"
              f"{tot:>9,}{100 * tot / grand:>7.1f}%")
    m = mix_effect(conn, latest)
    if m:
        print(f"\n#1-2 share of pounds: {100*m['share_now']:.1f}% now vs "
              f"{100*m['share_base']:.1f}% baseline "
              f"({m['baseline_years']} years, same ISO week)")
        print(f"#1 ${m['price_1']:.2f}  #1-2 ${m['price_1_2']:.2f}  "
              f"spread ${m['spread']:.2f}")
        print(f"blended ${m['actual']:.4f} vs ${m['counterfactual']:.4f} "
              f"at the baseline mix")
        print(f"MIX EFFECT ${m['effect']:+.4f}/cwt")
    conn.close()
