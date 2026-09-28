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


def grade_totals(cells):
    """{grade: {head, lbs, dollars, price}} summed across all brackets."""
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
    tot = grade_totals(cells)
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


# ---------------------------------------------------------------------------
# WHAT THE MERGED INDEX DAY IS ACTUALLY MADE OF
#
# CME folds Saturday and Sunday into the following Monday (Rule 10203.A.1), and
# recompute_fci_daily() folds them the same way on the way in: a Saturday sale
# is stored with report_date set to that Monday while raw_date keeps the true
# sale day. The 7-day window table therefore shows one "Sat 09/12-Mon 09/14"
# row and no standalone weekend rows, which is right.
#
# What that table cannot show is a weekend that contributed NOTHING, because
# nothing is exactly what a not-yet-fetched weekend also looks like. On Monday
# 2026-09-21 the newest index date on the page was Friday 09-18; the 07:43 run
# had exited 0 having ingested 475 rows across 84 locations and pushed clean,
# and AMS simply had published nothing dated 09-19, 09-20 or 09-21 yet. An
# absence and a pending fetch rendered identically, so there was no way to tell
# from the page whether the index was complete or still filling. That is this
# codebase's standing failure mode -- something failing to appear, silently --
# moved into the display layer.
#
# So state every constituent day, ZERO ONES INCLUDED, and keep two things apart:
#
#   * A sale reaches AMS the FOLLOWING day at the earliest (85% of a sale day's
#     head is fetchable by 07:30 the next morning, 95.8% by noon; El Reno runs a
#     median +1d 11:13 behind). Today's own sales therefore have not had time to
#     appear -- "not yet published", never "none".
#   * A day already past that point can be reported as having nothing ON
#     RECORD. Not as having had no sale: AMS still adds late reports, and CME's
#     own first print routinely omits them (see the publication gate tried and
#     reverted in update_index.py -- CME revised 9/3 upward by exactly the
#     Superior video volume). The wording stays "none reported".
#
# And an empty Saturday is the NORMAL case: measured 2026-09-21, only 4 of the
# 10 Saturdays before 09-19 produced any qualifying sale, every one of them from
# exactly one barn. Without that base rate beside it a zero reads as an outage
# every single week, and a weekly false alarm is a check nobody reads. The rate
# is computed live rather than quoted from this comment, because it will drift.
# ---------------------------------------------------------------------------

# Saturdays sampled for the base rate. Ten spans about two and a half months --
# long enough to be a rate, short enough that a barn dropping its Saturday sale
# shows up rather than being averaged away.
SATURDAY_BASE_RATE_N = 10


def merged_span_dates(index_date_iso):
    """
    The calendar days CME merges into one index date, oldest first.

    Monday absorbs the preceding Saturday and Sunday; every other index date
    stands alone. Mirrors shift_weekend_to_monday() in update_index.py -- if
    that ever changes, this must change with it.
    """
    d = date.fromisoformat(index_date_iso)
    if d.weekday() == 0:                       # Monday
        return [d - timedelta(days=2), d - timedelta(days=1), d]
    return [d]


def next_index_date(index_date_iso):
    """
    The next date that gets an index of its own. Saturday and Sunday do not --
    they merge into Monday -- so Friday's successor is Monday, not Saturday.
    """
    d = date.fromisoformat(index_date_iso) + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.isoformat()


def saturday_base_rate(conn, before_iso, n=SATURDAY_BASE_RATE_N):
    """
    How often the Saturdays BEFORE `before_iso` produced any qualifying sale,
    so a caller can say whether an empty one is unusual. A zero is only legible
    next to how often zero happens.

    Strictly before, never including the date asked about: the Saturday being
    described must not vote on whether it is normal, and on a Monday morning its
    own sales may still be arriving, which would bias the rate toward "empty".

    Returns None when mars_sales does not reach back far enough to sample at
    least half of n -- a base rate off three Saturdays is worse than none.
    """
    d = date.fromisoformat(before_iso)
    d -= timedelta(days=(d.weekday() - 5) % 7 or 7)   # the Saturday before it

    try:
        earliest = db.iso(conn.cursor().execute(
            "SELECT MIN(raw_date) FROM mars_sales").fetchone()[0])
    except Exception:
        return None
    if not earliest:
        return None
    earliest = date.fromisoformat(str(earliest)[:10])

    sats = []
    while len(sats) < n and d >= earliest:
        sats.append(d)
        d -= timedelta(days=7)
    if len(sats) < max(4, n // 2):
        return None

    rows = conn.cursor().execute(
        f"SELECT raw_date, location, head_count FROM mars_sales "
        f"WHERE raw_date BETWEEN {db.placeholders(1)} AND {db.placeholders(1)}",
        (sats[-1].isoformat(), sats[0].isoformat()),
    ).fetchall()
    per = {}
    for raw, loc, head in rows:
        e = per.setdefault(str(db.iso(raw)), {"head": 0, "barns": set()})
        e["head"] += int(head or 0)
        e["barns"].add((loc or "").strip().lower())

    sold = [per[s.isoformat()] for s in sats
            if per.get(s.isoformat(), {}).get("head")]
    return {
        "sampled": len(sats),
        "with_sales": len(sold),
        "empty": len(sats) - len(sold),
        "earliest": sats[-1].isoformat(),
        "latest": sats[0].isoformat(),
        "max_barns": max((len(s["barns"]) for s in sold), default=0),
        "head": sum(s["head"] for s in sold),
    }


def span_contributions(conn, index_date_iso, as_of=None,
                       saturdays=SATURDAY_BASE_RATE_N):
    """
    What each calendar day folded into `index_date_iso` contributed, INCLUDING
    the days that contributed nothing. Omitting those is the whole bug: a
    weekend with no sales and a weekend whose sales have not arrived both
    render as nothing at all, and the reader cannot tell which they are looking
    at.

    Buckets the same way recompute_fci_daily() does (shifted_bucket_date over
    report_date) and then splits that bucket by raw_date, the true sale day, so
    the per-day head sums exactly to the index date's own same_day_head --
    checked 2026-09-21: report_date 2026-09-14 is raw 09-12 (480 head, 1 barn)
    plus raw 09-14 (5,321 head, 11 barns), and fci_daily.same_day_head for
    09-14 is 5,801.

    Each day carries a status, and the distinction between two of them is the
    point of the function:

        reported  settled, and sales are on record
        none      settled, and none are on record -- NOT "no sale happened"
        pending   too early for a report to exist yet
        partial   too early to be complete, but something has already landed

    Returns {index_date, as_of, merged, days[], outside_span[], total_head,
    saturday_base_rate}.
    """
    index_date = date.fromisoformat(index_date_iso)
    if as_of is None:
        as_of = date.today()
    elif isinstance(as_of, str):
        as_of = date.fromisoformat(as_of[:10])

    span = merged_span_dates(index_date_iso)

    rows = conn.cursor().execute(
        f"SELECT report_date, raw_date, location, head_count FROM mars_sales "
        f"WHERE report_date BETWEEN {db.placeholders(1)} AND {db.placeholders(1)}",
        # Reach back a full week: a weekday snap can carry a report up to six
        # days forward (Thu -> the next Wed), and a row that snapped INTO this
        # index date from outside a narrow fetch would silently go unstated,
        # which is the failure this function exists to prevent.
        ((index_date - timedelta(days=7)).isoformat(),
         (index_date + timedelta(days=2)).isoformat()),
    ).fetchall()

    by_raw = {}
    for rd, raw, loc, head in rows:
        if shifted_bucket_date(loc, str(db.iso(rd))) != index_date_iso:
            continue
        c = by_raw.setdefault(str(db.iso(raw)),
                              {"head": 0, "rows": 0, "barns": set()})
        c["head"] += int(head or 0)
        c["rows"] += 1
        c["barns"].add((loc or "").strip().lower())

    days = []
    for d in span:
        c = by_raw.pop(d.isoformat(), None)
        head = c["head"] if c else 0
        # A sale is reported to AMS the following day at the earliest, so a day
        # not yet followed by a reporting day cannot be called empty -- only
        # unreported so far.
        settled = d < as_of
        days.append({
            "date": d.isoformat(),
            "label": d.strftime("%a %m/%d"),
            "weekday": d.strftime("%A"),
            "is_weekend": d.weekday() >= 5,
            "head": head,
            "barns": len(c["barns"]) if c else 0,
            "rows": c["rows"] if c else 0,
            "status": ("reported" if head else "none") if settled
                      else ("partial" if head else "pending"),
        })

    # Anything still bucketed here from OUTSIDE the calendar span -- a Clovis
    # Wednesday shifted to Thursday, an El Reno Tuesday snapped to Wednesday.
    # Surfaced rather than dropped: a function whose purpose is that nothing
    # goes unstated must not quietly swallow the rows it did not expect.
    outside = sorted(
        ({"date": k, "head": v["head"], "barns": len(v["barns"]),
          "rows": v["rows"]} for k, v in by_raw.items()),
        key=lambda r: r["date"])

    return {
        "index_date": index_date_iso,
        "as_of": as_of.isoformat(),
        "merged": len(span) > 1,
        "days": days,
        "outside_span": outside,
        # Everything on record for this bucket, span days and bucketed-in rows
        # together. Computed here rather than in each dashboard because it is
        # the value that decides whether the page reports a normal day or an
        # outage, and the two app.py copies must not be able to disagree about
        # it. Zero means nothing has been reported for ANY constituent day --
        # which is the state the first version of the weekend line described as
        # "complete as far as AMS has reported".
        "total_head": (sum(d["head"] for d in days)
                       + sum(o["head"] for o in outside)),
        "saturday_base_rate": saturday_base_rate(conn, span[0].isoformat(),
                                                 saturdays),
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

    # The merged day, stated day by day -- including the days that gave
    # nothing, which is the whole point. Run for the next index date if one is
    # due but not yet printed, otherwise for the newest printed one.
    pending = next_index_date(latest)
    target = pending if date.fromisoformat(pending) <= date.today() else latest
    sc = span_contributions(conn, target)
    print(f"\nmerged span for {target} (as of {sc['as_of']}):")
    for day in sc["days"]:
        print(f"  {day['label']}  {day['status']:<9}{day['head']:>7,} head  "
              f"{day['barns']} barn(s)")
    for extra in sc["outside_span"]:
        print(f"  + bucketed in from {extra['date']}: {extra['head']:,} head")
    # Say the absence out loud here too. A CLI that prints three "none" lines
    # and then a cheerful base rate has the same failure mode as the page did.
    if not sc["total_head"]:
        print(f"  *** NOTHING ON RECORD for any day of {target} ***")
    br = sc["saturday_base_rate"]
    if br and sc["total_head"]:
        print(f"  Saturday base rate: {br['with_sales']} of the last "
              f"{br['sampled']} Saturdays sold ({br['earliest']}..{br['latest']}), "
              f"at most {br['max_barns']} barn(s)")
    conn.close()
