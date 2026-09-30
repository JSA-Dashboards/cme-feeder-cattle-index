"""
A census of mars_sales against what USDA AMS serves TODAY. REPORT ONLY:
# this module deletes nothing from mars_sales, ever

WHY IT EXISTS. mars_sales is written only through db.merge_ignore() -- insert
if absent, never touch an existing row -- so when AMS withdraws or revises a
lot, our copy keeps the old version forever. recompute_fci_daily() reads that
table with NO WHERE CLAUSE, so the stale row is treated as index-qualifying in
perpetuity and the published index is wrong until a human notices. It has
happened twice in 2 years 7 months, and both were found the same way: by an
unscoped comparison against what AMS serves now.

  MITCHELL SD, a WITHDRAWAL. AMS published slug 2022's 09/17/2026 report at
  06:53 on 09/18, served FOUR qualifying steer lots to the 07:43 run and THREE
  to the 13:00 run the same day. The phantom fourth -- 850 bracket, grade 1-2,
  6 head at $275.00 against that barn's own $336-353 that morning -- cost 2
  cents on 09-17 and 09-18 and sat in the table until a human removed it.

  McALESTER OK, a REVISION. AMS published slug 1827's 09/22/2026 report with a
  750-bracket grade-1 lot of 15 head at $328.32, then corrected it to 14 head
  at $330.29. merge_ignore inserted the correction and could not remove what it
  replaced, so we held BOTH -- five lots where AMS serves four. Those 15 phantom
  head moved every index date whose 7-day window touched 09-22: 09-22 was
  -0.0093 against CME, 09-23 -0.0062 and 09-24 -0.0081, and removing the row
  took all three to about -0.001.

So the expected output of this module is EMPTY almost every day, and a
non-empty one is worth interrupting someone for. That is the product. Every
decision below is made to protect it, because a census that prints two lines
every morning gets ignored inside a week and then the real one is invisible.

WHY A DETECTOR AND NOT A FIXER. Three attempts at an automatic delete were
built and reverted, and the third explained why. At one event per two and a
half years no threshold in such a module can be calibrated against data, and
the guards that make deleting safe are the same guards that refuse a real
withdrawal: the last build's correction-flag gate WITHHELD the Mitchell case
and its share cap REFUSED the McAlester one -- 0 of 2 -- while it still carried
a blocker that could have zeroed the whole ingest. Reporting has no data-loss
risk, so it needs none of those gates, and catches both.

It also does not shorten time-to-CORRECT. A phantom stays in mars_sales, and
stays in the published index, until a human removes it by hand exactly as
Mitchell and McAlester were. What this buys is time-to-NOTICE: one morning,
instead of days and an accident.

NO THRESHOLDS. Every knob the delete path needed -- rows per group, share of a
day's head, groups per run, head per run, a grace period below CME's last
print, AMS's own corrections flag -- existed to bound data loss. With nothing
destroyed there is no loss to bound, so all six are gone rather than retuned.
The only three numbers here are derived rather than chosen:

    6   detect_final_sale_day()'s maximum reach, Monday to Sunday. Pinned by a
        test over all seven weekdays, not picked. See judged_window().
    ==  stats.returnedRows == stats.totalRows. An equality on a signal AMS
        supplies, not a tolerance on one we invented.
    0   a slug holding nothing in the judged window is not "withheld" --
        nothing is at stake, so there is nothing to withhold.

THE SHAPE, which is reconcile.py's split kept even though the module it split
was not:

    compare()     PURE. No network, no database, no clock. Every acceptance
                  test and every mutant runs against this.
    run_census()  the IO shell -- reads mars_sales, calls compare(), replaces
                  this module's own two result tables.
    __main__      standalone. Walks the roster itself, prints, and WRITES
                  NOTHING, so a by-hand wide sweep can never overwrite the
                  pipeline's statement about the 14-day window with a statement
                  about a different window.

WHAT IT CANNOT SEE, stated plainly rather than implied away. The piggybacked
run judges sale dates in roughly [today-8, today] -- see judged_window() for
where the 8 comes from. A row that goes stale MORE THAN ABOUT EIGHT DAYS after
its own sale date is never seen by any scheduled run, ever. McAlester was 6
days stale when found and Mitchell 1, so both sit inside that window, but two
data points are not a guarantee. The standalone with --since is the only route
to anything older, and it is a human action rather than a schedule: widening
REFETCH_LOOKBACK_DAYS is free in HTTP but changes what the INGEST parses and
merges, which is a deliberate decision for a human and not a side effect of a
diagnostic.
"""
import argparse
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

import snowflake_db as db
from bucketing import shifted_bucket_date
from update_index import derived_dates, qualifying_rows

# detect_final_sale_day()'s maximum reach: a narrative naming a later weekday
# can move a Monday report to the Sunday after it, and no further. DERIVED, not
# chosen -- tests/test_mars_census.py walks all seven weekdays and asserts no
# combination moves a date by more than this, so the constant is pinned to its
# source and cannot quietly become a tuning knob.
MAX_DATE_SHIFT_DAYS = 6

PHANTOM = "phantom"
MISSING = "missing"
WITHHELD = "withheld"
NOTE = "note"


def _date(v) -> date:
    """A date, datetime or ISO string as a date. The backends disagree."""
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v).strip()[:10])


def _iso(v) -> str:
    return _date(v).isoformat()


def price_cents(p) -> int:
    """
    A price as integer CENTS.

    AMS serves 353.72 on one row and a bare int 490 on the next, head_count can
    arrive as a string, and SQLite hands back a REAL where Snowflake's
    connector hands back something else. Comparing those raw is how a key set
    silently fails to intersect -- and an empty intersection here reads as "AMS
    withdrew the whole day". Through Decimal(str(p)) so that 275, 275.0,
    "275.00" and Decimal("275") are one key, and no float comparison ever
    decides whether a row gets reported.
    """
    return int(round(float(Decimal(str(p))) * 100))


def key_of(report_date, slug_id, weight_low, muscle_grade, avg_price, head_count):
    """
    mars_sales' PRIMARY KEY, normalised so the stored side and the served side
    produce the same tuple for the same lot.

    KEYED ON report_date, NOT raw_date, because report_date is what the stored
    key carries -- the weekend-shifted one. Ericson NE (slug 1853) is the live
    proof: its most recent group is report_date 2026-09-14 (Monday) over
    raw_date 2026-09-12 (Saturday). A served row keyed on its true Saturday
    date would match nothing at all and every one of those five stored rows
    would read as a phantom.
    """
    return (_iso(report_date), int(slug_id), int(weight_low),
            str(muscle_grade).strip(), price_cents(avg_price), int(head_count))


@dataclass(frozen=True)
class StoredRow:
    """One row of mars_sales, with the key and the index date it feeds."""
    key: tuple
    report_date: str
    raw_date: str
    slug_id: int
    location: str
    weight_low: int
    muscle_grade: str
    head_count: int
    avg_weight: float
    avg_price: float
    index_date: str


@dataclass(frozen=True)
class Finding:
    """
    One line of the census. `kind` is the only thing that says which of the
    four propositions it asserts, and they are never summed or shown under one
    word -- see Findings.
    """
    kind: str
    slug_id: int
    detail: str
    location: str | None = None
    raw_date: str | None = None
    report_date: str | None = None
    index_date: str | None = None
    weight_low: int | None = None
    muscle_grade: str | None = None
    head_count: int | None = None
    avg_weight: float | None = None
    avg_price: float | None = None

    def describe(self) -> str:
        if self.weight_low is None:
            return f"slug {self.slug_id} {self.location or ''}".strip()
        return (f"slug {self.slug_id} {self.location} sale {self.raw_date} "
                f"(report {self.report_date}, index {self.index_date}) "
                f"{self.weight_low} lb grade {self.muscle_grade}, "
                f"{self.head_count} head @ ${self.avg_price}")


@dataclass(frozen=True)
class Findings:
    """
    The verdict. FOUR LISTS AND NO SCALAR KNOBS.

      phantom   stored rows whose key is in NO raw served row for that slug.
                The point of the module.
      missing   served QUALIFYING rows whose key is in no stored row for that
                slug. A different fact, reported separately and never added to
                the above -- see compare() for why the two use different sets,
                and note that the same column means two different things in
                the two modes: piggybacked it can only be merge_ignore having
                DECLINED a row (a primary-key collision), standalone it is a
                genuine late publisher.
      withheld  (slug, reason) where the payload could not be believed OR
                never arrived at all, AND we hold rows in the judged window.
                The check going dark, made visible instead of silent.
      notes     served, still, but no longer qualifying. NOT a phantom and NOT
                an alarm: it does not move the headline counts. It is carried
                and written anyway because the log is not read, and a fact
                that exists only in a log may as well not exist.
    """
    window_start: str
    window_end: str
    n_compared: int
    phantom: tuple = ()
    missing: tuple = ()
    withheld: tuple = ()
    notes: tuple = ()

    @property
    def rows(self):
        return self.phantom + self.missing + self.withheld + self.notes


def judged_window(since, until):
    """
    The sale dates this comparison is entitled to an opinion about, as
    (lo, hi).

    The query filters on AMS's OWN report_begin_date, but a stored row is keyed
    by a raw_date that detect_final_sale_day() may have moved FORWARD by up to
    MAX_DATE_SHIFT_DAYS. A stored group at sale date R was therefore produced
    by a report dated somewhere in [R-6, R], so R can only be judged when that
    whole span sat inside the query. Otherwise a report we never asked for
    looks exactly like a lot AMS withdrew -- and a whole sale day arriving as
    phantoms is the one false positive that would make this module worth
    ignoring.

    lo = since + 6, not lo = since. Against the pipeline's 14-day refetch that
    is the difference between claiming [today-14, today] and judging
    [today-8, today], and the six days it gives up are six days we were never
    entitled to an opinion about.

    BOTH DIRECTIONS USE THIS ONE WINDOW, including `missing`, where absence of
    evidence is not the argument and a wider bound would technically be sound.
    The panel makes one statement about one window; a finding outside the
    window it claims to have judged is a finding nobody can place. The
    standalone widens the window rather than the rules.
    """
    return _date(since) + timedelta(days=MAX_DATE_SHIFT_DAYS), _date(until)


NOT_FETCHED = (
    "no payload arrived for this slug at all -- the fetch raised, so the "
    "roster loop skipped it and nothing is known about what AMS serves here "
    "now")


def _eligible(served_payloads, roster_slugs):
    """
    Every slug this run is entitled to say anything about, judged or withheld.

    THE ROSTER IS THE AUTHORITY ON WHAT SHOULD HAVE BEEN FETCHED, not the
    payloads dict. update_index.py's roster loop drops a slug from
    census_payloads when its fetch raises, so deriving scope from the payloads
    alone made a FAILED FETCH INDISTINGUISHABLE FROM A CLEAN ONE: the slug was
    never judged, produced no finding of any kind, and the summary line then
    printed an affirmative all-clear over rows nobody had checked. Reproduced
    with the Mitchell phantom in the table and slug 2022 dropped from the
    payloads -- "0 stored row(s) AMS no longer serves ... 0 slug(s) withheld",
    on a morning when the phantom was sitting in the index.

    A truncated payload already became a withheld finding. An absent one is
    the same proposition -- the check went dark for this slug -- and it now
    gets the same treatment, with a reason of its own.

    roster_slugs=None keeps the old scope, which is what the unit tests and
    any caller without a roster want: nothing to add, so eligibility is the
    payloads. The union, rather than the roster alone, because the payloads
    are direct evidence that a slug WAS fetched and a roster edit must never
    silently stop a fetched slug from being judged.
    """
    slugs = {int(k) for k in (served_payloads or ())}
    if roster_slugs is not None:
        slugs |= {int(k) for k in roster_slugs}
    return slugs


def _disbelief(raw, stats):
    """
    Why this payload cannot be believed, or None.

    Two conditions, each an existence or an equality test rather than a
    tolerance. An unbelievable payload has no opinion about what AMS still
    serves, so the slug's stored rows are not compared at all -- they are
    reported as withheld WITH THE REASON, because a check that goes dark
    silently is the failure shape this repository keeps getting bitten by.
    """
    if not raw:
        return ("AMS served no rows at all for this slug -- an error payload, "
                "or a report-family rename that empties every slug at once")
    returned, total = stats.get("returnedRows"), stats.get("totalRows")
    if returned is None or total is None:
        return ("the response carried no stats.returnedRows/totalRows, so the "
                "payload's completeness cannot be established")
    if returned != total:
        return (f"AMS returned {returned} of {total} rows -- a truncated "
                f"report is indistinguishable from a withdrawal")
    return None


def compare(stored_groups, served_payloads, since, until, locations=None,
            roster_slugs=None):
    """
    The verdict on everything the run set out to fetch. PURE: no network, no
    database, no clock.

    THE TWO DIRECTIONS USE DIFFERENT SETS, AND THE ASYMMETRY IS THE POINT. Each
    makes the OTHER side as generous as it can, because that is what suppresses
    false positives:

        phantom = stored_keys - RAW served keys          (a strict SUPERSET on
                                                          the right -> fewer)
        missing = QUALIFYING served keys - stored_keys   (a strict SUBSET on
                                                          the left -> fewer)

    PHANTOMS SUBTRACT FROM THE RAW SERVED SET, built before qualifying_rows()
    is applied. This is the fix for the previous build's worst failure: it
    derived "what AMS serves" from the QUALIFYING rows, so a revision that
    nulled avg_weight on some lots while a sibling still qualified kept the
    group live and then destroyed the affected rows as withdrawn -- 182 head
    gone while AMS was serving every one, under a log line reading "AMS no
    longer serves this lot". Two different propositions had been collapsed into
    one set. Here they are two sets with two provenances, and a stored key that
    IS served but no longer qualifies becomes a note, never a phantom.

    Subtracting from the raw set is safe in the only direction that matters:
    raw rows include heifers, 1100 lb Large-frame steers and 350 lb calves
    whose keys can never equal a stored key, because mars_sales holds only
    brackets 700/750/800/850 (enforced structurally by
    tests/test_index_isolation.py). A superset on the right can only produce
    FEWER findings.

    MISSING MUST USE THE QUALIFYING SET. Subtracting stored from the raw served
    set would report every heifer and every 1100 lb lot as "we do not hold
    this", which is true and useless -- the ingest is supposed not to hold
    them.

    THE SERVED SET IS SLUG-WIDE, NOT GROUP-SCOPED, and so is the stored set it
    is compared against. A stored row whose key matches ANY raw row in that
    slug's payload is still served, possibly refiled under a different sale
    day, and is not a phantom. That generosity costs one real capability and
    buys another: a lot a narrative revision RE-DATES changes report_date and
    therefore changes its key, so the old copy surfaces as a phantom while the
    new copy sits beside it -- the double-count the delete path explicitly
    could not reach.

    ELIGIBILITY IS STRUCTURAL. Only the auction roster is ever fetched, so only
    roster slugs -- and slugs a payload actually arrived for -- can be judged
    or withheld. Direct-trade and video slugs, which serve the current week
    only, so "AMS withdrew it" and "we never asked" are the same observation,
    can never enter the comparison. mars_sales holds 101 distinct slugs against
    the roster's 89; the extra twelve are exactly those merge-only sources, and
    deriving scope from the roster rather than from SELECT DISTINCT slug_id is
    what keeps them out.

    AN ELIGIBLE SLUG WITH NO PAYLOAD IS WITHHELD, NOT SKIPPED. See _eligible():
    a fetch that raised leaves the slug out of the payloads dict, and treating
    that as "nothing to say" let an all-clear cover rows that were never
    checked.
    """
    locations = locations or {}
    served_payloads = {int(k): (v or {}) for k, v in (served_payloads or {}).items()}
    eligible = _eligible(served_payloads, roster_slugs)
    lo, hi = judged_window(since, until)
    phantom, missing, withheld, notes = [], [], [], []

    judged = {}
    keys_by_slug = {}
    for (slug_id, raw_date), rows in stored_groups.items():
        slug_id = int(slug_id)
        keys_by_slug.setdefault(slug_id, set()).update(r.key for r in rows)
        if slug_id in eligible and lo <= _date(raw_date) <= hi:
            judged[(slug_id, _iso(raw_date))] = tuple(rows)

    compared = set()
    for slug_id in sorted(eligible):
        payload = served_payloads.get(slug_id, {})
        raw = payload.get("results") or []
        stats = payload.get("stats") or {}
        name = locations.get(slug_id)
        at_stake = sorted(g for g in judged if g[0] == slug_id)

        reason = NOT_FETCHED if slug_id not in served_payloads \
            else _disbelief(raw, stats)
        if reason:
            # SCOPED TO WHAT IS AT STAKE, and that is not a threshold -- it is
            # "nothing is at stake". Measured 2026-09-28: 10 of 89 slugs
            # returned an empty payload and every one of the ten holds ZERO
            # rows in the judged window (Ericson last sold 09-12, La Junta
            # 2026-03-24, McCook 2025-09-29, two have never sold at all). They
            # are barns that did not sell in the window. Reporting them would
            # put ten noise lines on the page every morning, and noise gets
            # ignored -- which would cost the one line that matters.
            #
            # The same scoping covers a slug whose fetch RAISED. A barn we hold
            # nothing for in the judged window has nothing at stake whatever
            # became of its payload; a barn we hold rows for has everything at
            # stake, and that is the case that used to vanish.
            if at_stake:
                withheld.append(Finding(
                    kind=WITHHELD, slug_id=slug_id, location=name,
                    detail=f"{reason}. {len(at_stake)} sale day(s) held here "
                           f"in the judged window were NOT judged."))
            continue

        served_keys, unkeyable = set(), 0
        for r in raw:
            try:
                report_iso, _ = derived_dates(r)
                served_keys.add(key_of(report_iso, slug_id, r["weight_break_low"],
                                       r["muscle_grade"], r["avg_price"],
                                       r["head_count"]))
            except Exception:       # noqa: BLE001 -- annotated below, not a gate
                unkeyable += 1

        # AN UN-KEYABLE SERVED ROW IS AN ANNOTATION, NOT A GATE. A served row
        # missing a key field cannot be matched, so a stored row corresponding
        # to it could read as a phantom. The delete path made this a hard
        # refusal; for a detector that is an overreaction, because there is no
        # loss to prevent and a false line costs a human one minute. Measured
        # on the two case slugs: 56 un-keyable raw rows on 1827 and 7 on 1853,
        # and ZERO of them index-shaped on either. So the count rides along on
        # every phantom finding and nothing is suppressed.
        qualifying = {}
        for r in qualifying_rows(raw):
            try:
                report_iso, raw_iso = derived_dates(r)
            except Exception:       # noqa: BLE001 -- same annotation
                unkeyable += 1
                continue
            qualifying[key_of(report_iso, slug_id, r["weight_break_low"],
                              r["muscle_grade"], r["avg_price"],
                              r["head_count"])] = (report_iso, raw_iso, r)

        seen = f"AMS serves {len(raw)} row(s) for slug {slug_id} over " \
               f"{_iso(since)}..{_iso(until)}"
        aside = (f" {unkeyable} served row(s) on this slug could not be keyed."
                 if unkeyable else "")

        for group in at_stake:
            compared.add(group)
            for row in judged[group]:
                if row.key in served_keys:
                    if row.key not in qualifying:
                        notes.append(_finding(NOTE, row, (
                            "AMS still SERVES this lot but it no longer "
                            "qualifies for the index -- a revision may have "
                            "nulled a field qualifying_rows() requires. Not a "
                            "phantom: the row is served.")))
                    continue
                phantom.append(_finding(PHANTOM, row, (
                    f"{seen} and this lot is in none of them.{aside}")))

        stored_keys = keys_by_slug.get(slug_id, set())
        for k in sorted(qualifying):
            report_iso, raw_iso, r = qualifying[k]
            if not (lo <= _date(raw_iso) <= hi):
                continue
            compared.add((slug_id, raw_iso))
            if k in stored_keys:
                continue
            where = name or r.get("market_location_city") or r.get("report_title")
            missing.append(Finding(
                kind=MISSING, slug_id=slug_id, location=where,
                raw_date=raw_iso, report_date=report_iso,
                index_date=shifted_bucket_date(where, report_iso),
                weight_low=int(r["weight_break_low"]),
                muscle_grade=str(r["muscle_grade"]),
                head_count=int(r["head_count"]),
                avg_weight=float(r["avg_weight"]),
                avg_price=float(r["avg_price"]),
                detail="AMS serves this qualifying lot and no stored row of "
                       "this slug matches it."))

    return Findings(
        window_start=lo.isoformat(), window_end=hi.isoformat(),
        n_compared=len(compared), phantom=tuple(phantom),
        missing=tuple(missing), withheld=tuple(withheld), notes=tuple(notes))


def _finding(kind, row: StoredRow, detail: str) -> Finding:
    return Finding(kind=kind, slug_id=row.slug_id, location=row.location,
                   raw_date=row.raw_date, report_date=row.report_date,
                   index_date=row.index_date, weight_low=row.weight_low,
                   muscle_grade=row.muscle_grade, head_count=row.head_count,
                   avg_weight=row.avg_weight, avg_price=row.avg_price,
                   detail=detail)


# ---------------------------------------------------------------------------
# The IO shell.
# ---------------------------------------------------------------------------

def load_stored_groups(conn, slug_ids, since, until):
    """
    {(slug_id, raw_date): (StoredRow, ...)} over the fetch window, for the
    slugs this run set out to fetch.

    `slug_ids` IS THE ELIGIBLE SET, NOT THE PAYLOAD KEYS. A slug whose fetch
    raised has to be loaded too, or "we hold rows here and nobody checked
    them" cannot be established and the withheld finding for it never fires --
    the load would have quietly agreed that nothing was at stake.

    GROUPED BY raw_date -- the true sale day -- but each row KEYED ON
    report_date, which is the stored primary key and carries CME's weekend
    shift on top of raw_date. See key_of() for the Ericson case that makes the
    distinction load-bearing rather than pedantic.

    The read is the whole window rather than the judged sub-window, because
    the stored side of `missing` is deliberately slug-wide: a served row
    refiled under a neighbouring sale day must still count as held.
    """
    slug_ids = {int(s) for s in slug_ids}
    if not slug_ids:
        return {}
    p = db.placeholders(1)
    rows = conn.cursor().execute(
        "SELECT report_date, raw_date, slug_id, location, weight_low, "
        "muscle_grade, head_count, avg_weight, avg_price FROM mars_sales "
        f"WHERE raw_date >= {p} AND raw_date <= {p}",
        (_iso(since), _iso(until)),
    ).fetchall()

    groups = {}
    for r in rows:
        (report_date, raw_date, slug_id, location, weight_low,
         muscle_grade, head_count, avg_weight, avg_price) = db.iso_row(r)
        slug_id = int(slug_id)
        if slug_id not in slug_ids:
            continue
        report_date, raw_date = _iso(report_date), _iso(raw_date)
        groups.setdefault((slug_id, raw_date), []).append(StoredRow(
            key=key_of(report_date, slug_id, weight_low, muscle_grade,
                       avg_price, head_count),
            report_date=report_date, raw_date=raw_date, slug_id=slug_id,
            location=location, weight_low=int(weight_low),
            muscle_grade=str(muscle_grade), head_count=int(head_count),
            avg_weight=float(avg_weight), avg_price=float(avg_price),
            index_date=shifted_bucket_date(location, report_date)))
    return {g: tuple(rs) for g, rs in groups.items()}


def init_tables(conn):
    """
    This module's own two tables. Schema convention matches the rest of the
    app -- snowflake/01_schema.sql owns the Snowflake side.
    """
    if db.use_snowflake():
        return
    # THE RUN ROW IS A SEPARATE POSITIVE FACT, and that is the whole answer to
    # "an empty result must be visibly empty rather than absent". A findings
    # table alone cannot tell "0 phantoms" from "the census did not run" --
    # both are zero rows. This says: the check ran, at this time, over this
    # window, and compared this many sale days. One table with a kind='clean'
    # sentinel row would be the same information and exactly the cleverness
    # that lets a future bug merge the reassurance with the silence.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mars_census_runs (
            run_at TEXT NOT NULL,
            window_start TEXT NOT NULL,
            window_end TEXT NOT NULL,
            n_compared INTEGER NOT NULL,
            n_phantom INTEGER NOT NULL,
            n_missing INTEGER NOT NULL,
            n_withheld INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mars_census (
            kind TEXT NOT NULL,
            slug_id INTEGER NOT NULL,
            location TEXT,
            raw_date TEXT,
            report_date TEXT,
            index_date TEXT,
            weight_low INTEGER,
            muscle_grade TEXT,
            head_count INTEGER,
            avg_weight REAL,
            avg_price REAL,
            detail TEXT
        )
    """)
    conn.commit()


def run_census(conn, payloads, since, until, locations=None, roster_slugs=None):
    """
    Compare, then replace this module's two tables. Returns the Findings.

    `roster_slugs` is what the caller SET OUT to fetch. Passing it is what
    lets a slug whose fetch raised come back as withheld instead of vanishing;
    see _eligible(). Omitting it keeps the old scope.

    BOTH TABLES ARE REPLACED WHOLESALE, through db.truncate() -- which is
    recompute_fci_daily()'s own precedent, and which is why the literal token
    for a row-removing statement does not appear in this file at all. The
    constraint is satisfied literally rather than by argument, so the guard in
    tests/test_index_isolation.py can be strict rather than fuzzy.

    NO HISTORY is kept. A finding persists every run until a human fixes it, so
    history buys little and costs a table that grows and is pushed nightly.
    That is a tradeoff, stated rather than hidden.
    """
    init_tables(conn)
    payloads = {int(k): v for k, v in (payloads or {}).items()}
    eligible = _eligible(payloads, roster_slugs)
    stored = load_stored_groups(conn, eligible, since, until)
    findings = compare(stored, payloads, since, until, locations, eligible)

    db.truncate(conn, "mars_census")
    db.truncate(conn, "mars_census_runs")
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO mars_census_runs (run_at, window_start, window_end, "
        "n_compared, n_phantom, n_missing, n_withheld) "
        f"VALUES ({db.placeholders(7)})",
        (datetime.now().isoformat(timespec="seconds"), findings.window_start,
         findings.window_end, findings.n_compared, len(findings.phantom),
         len(findings.missing), len(findings.withheld)))
    for f in findings.rows:
        cur.execute(
            "INSERT INTO mars_census (kind, slug_id, location, raw_date, "
            "report_date, index_date, weight_low, muscle_grade, head_count, "
            f"avg_weight, avg_price, detail) VALUES ({db.placeholders(12)})",
            (f.kind, f.slug_id, f.location, f.raw_date, f.report_date,
             f.index_date, f.weight_low, f.muscle_grade, f.head_count,
             f.avg_weight, f.avg_price, f.detail))
    conn.commit()
    return findings


def summary_line(findings) -> str:
    """
    The one line that prints every run, findings or none. "0 phantoms" is the
    reassurance; a missing line is indistinguishable from a check that did not
    run.
    """
    return (f"AMS census -- {findings.n_compared} sale day(s) in "
            f"{findings.window_start}..{findings.window_end} checked against "
            f"USDA: {len(findings.phantom)} stored row(s) AMS no longer "
            f"serves, {len(findings.missing)} served row(s) we do not hold, "
            f"{len(findings.withheld)} slug(s) withheld")


def report_lines(findings):
    """
    The census as a list of strings -- the same shape barn_report.report_lines()
    returns, and consumed the same way: the caller prints it inside a guard.

    THE FOUR KINDS GET FOUR HEADINGS AND FOUR WORDINGS. They are never summed
    and never shown under one label. "We hold a row AMS does not serve" and
    "AMS serves a row we do not hold" are opposite facts with opposite fixes,
    and the previous build's log asserted something false precisely by
    describing both with one sentence.
    """
    lines = [summary_line(findings)]
    for bucket, heading in (
            (findings.phantom,
             "  AMS NO LONGER SERVES THESE -- we hold them and the index "
             "counts them:"),
            (findings.missing,
             "  AMS SERVES THESE AND WE DO NOT HOLD THEM -- the opposite "
             "direction, not to be added to the above:"),
            (findings.withheld,
             "  WITHHELD -- no payload arrived, or the one that did could not "
             "be believed, so these slugs were NOT CHECKED. This is not a "
             "clean result for them:"),
            (findings.notes,
             "  STILL SERVED, NO LONGER QUALIFYING -- not phantoms:")):
        if not bucket:
            continue
        lines.append(heading)
        for f in bucket:
            lines.append(f"    {f.describe()}")
            lines.append(f"      {f.detail}")
    return lines


# ---------------------------------------------------------------------------
# Standalone. Prints, and writes NOTHING.
# ---------------------------------------------------------------------------

def _standalone(since, until):
    """
    Walk the roster and compare, reading mars_sales and writing nothing.

    THE WRITE IS DELIBERATELY ABSENT rather than optional. This is the unscoped
    sweep that found both real cases, and its window is whatever a human
    typed -- so letting it touch the tables would let a statement about
    2024-2026 silently replace the pipeline's statement about the last
    fortnight, on a panel captioned with the pipeline's window.

    A wide sweep can also be silently truncated: AMS reports
    userAllowedRows = 100000, and a 2.5-year window across a busy slug could
    reach it. returnedRows != totalRows is exactly the detector for that, and
    those slugs come back as WITHHELD rather than as a flood of phantoms -- so
    read the withheld list, not just the phantom list.
    """
    import update_index as ui

    auth = ui.get_auth()
    roster = json.loads(ui.ROSTER_PATH.read_text(encoding="utf-8"))
    since_str, until_str = ui.mdY(_date(since)), ui.mdY(_date(until))

    payloads, locations = {}, {}
    # The roster, kept whatever happens below. A slug that raises is NOT
    # dropped from the scope -- it stays eligible and comes back WITHHELD, the
    # same as a truncated payload, because "we did not manage to look" and
    # "we looked and it was clean" must never print the same.
    roster_slugs = [int(loc["slug_id"]) for loc in roster]
    for loc in roster:
        slug_id = loc["slug_id"]
        locations[int(slug_id)] = loc["city"] or loc["title"]
        try:
            payloads[slug_id] = ui.fetch_slug_payload(
                slug_id, since_str, until_str, auth)
        except Exception as e:                  # noqa: BLE001
            print(f"  [skip] slug {slug_id} ({loc['title']}): {e}")

    conn = db.get_conn()
    try:
        stored = load_stored_groups(
            conn, _eligible(payloads, roster_slugs), since, until)
    finally:
        conn.close()
    return compare(stored, payloads, since, until, locations, roster_slugs)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Report every stored mars_sales row AMS no longer serves. "
                    "Reports only -- nothing here removes or rewrites a row.")
    ap.add_argument("--since", required=True,
                    help="ISO date to compare from. Sale dates before "
                         "since+6 are not judged; see judged_window().")
    ap.add_argument("--until", default=None,
                    help="ISO date to compare to (default: today)")
    a = ap.parse_args()
    _until = date.fromisoformat(a.until) if a.until else date.today()
    for _line in report_lines(_standalone(date.fromisoformat(a.since), _until)):
        print(_line)
