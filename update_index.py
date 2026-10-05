"""
Extends the CME Feeder Cattle Index dashboard's history past where the Ross
workbook stops (2026-01-23) using USDA AMS MARS API data.

Methodology matches CME's own published definition (cmegroup.com): the
index is a rolling SEVEN-CALENDAR-DAY volume-weighted average, not a
same-day snapshot. Every qualifying sale row (Steers, Medium & Large
frame, grade #1 or #1-2, 700-899 lb weight brackets, final reports only —
preliminary excluded) is pulled from a fixed roster of ~60 sale-barn
reports across the CME 12-state region. For each date D:

    FCI(D) = sum(head*weight*price for report_date in [D-6, D])
             / sum(head*weight for report_date in [D-6, D])

Every pound gets equal weight (CME's own wording). Using a single day
instead of the 7-day window was an earlier bug here — with ~60 sale-barn
locations, many reporting only weekly, a single day's sample is thin
(sometimes 1 location), which produced day-to-day noise far larger than
CME's real index shows.

Also pulls Direct Cattle Report PDFs (see direct_reports.py) for the
Direct/Video/Internet trade component of CME's sample -- NOT exposed as
structured data via the MARS API (narrative text only for that report
family), so these are fetched and parsed directly from
ams.usda.gov/mnreports/. Only "Current"-timing, FOB-freight rows qualify
(CME's 14-day pickup / FOB rule); forward-month contracts and delivered
(non-FOB) rows are excluded. These PDFs always show the current week only
(no historical-date parameter), so this component only extends the
dataset forward from whenever it's first run -- it doesn't backfill past
dates the way the auction data's initial run did.

Run manually or on a schedule:
    python update_index.py [--since YYYY-MM-DD]

Requires env var MARS_API_KEY (USDA MARS API key, free registration at
https://mymarketnews.ams.usda.gov/mymarketnews-api).
"""
import argparse
import json
import os
import re
import threading
from datetime import date, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

import snowflake_db as db
from direct_reports import DIRECT_REPORT_SLUGS, fetch_all_direct_rows
from bucketing import check_bucket_drift, shifted_bucket_date
from snapshots import capture_snapshots
from video_reports import (VIDEO_MAX_AGE_DAYS, VIDEO_REPORT_SLUGS,
                           fetch_all_video_rows)

HERE = Path(__file__).parent
DATA_DIR = HERE / "data"
ROSTER_PATH = DATA_DIR / "mars_roster.json"
DB_PATH = DATA_DIR / "mars_history.db"

MARS_BASE = "https://marsapi.ams.usda.gov/services/v1.2"

# How far back of already-stored dates each run re-asks USDA for. Every run
# re-fetches this window in full and upserts, so a report that USDA publishes
# LATE is only ever picked up if it lands inside it -- past that, no run asks
# for that date again and the sale is invisible for good.
#
# Measured 2026-09-09 over 80 auctions and 246 reports: 98.3% of qualifying head
# is published within 2 days of the sale and 99.9% within 3, but the tail is
# real -- Roswell published 8 days late and Mid Missouri Stockyards 12 (72 head
# between them, 0.13%). 7 days missed both. 14 covers everything observed with
# room to spare.
#
# Free to widen, which is why it is 14 and not 8: the window is a QUERY
# PARAMETER on one call per auction slug, so a wider one costs no extra
# requests, and the two expensive stages -- fetch_all_direct_rows() and
# fetch_all_video_rows(), which run pdfplumber over ~20 PDFs and dominate the
# ~20 minute runtime -- take no date range at all and are completely unaffected.
# The only cost is parsing more JSON rows per slug.
REFETCH_LOOKBACK_DAYS = 14
TARGET_GRADES = {"1", "1-2"}
TARGET_BRACKETS = {700, 750, 800, 850}
CONTINUATION_START = date(2026, 1, 24)  # day after the workbook's last date

_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_SOLD_ON_RE = re.compile(
    r"\bsold\s+(?:on\s+)?(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b"
)


def detect_final_sale_day(report_date: date, narrative: str | None) -> date:
    """
    CME's rule: a multi-day sale without separate per-day reports must be
    attributed to its FINAL day, not whichever day USDA's own report_date
    field happens to carry. USDA's report_begin_date/report_end_date fields
    do NOT reliably reflect this -- confirmed on a real report (El Reno OK,
    9/1/26) where both fields said "09/01" (Tuesday) despite the report's
    own narrative explicitly describing sales on both Tuesday AND
    Wednesday. Detects this from the narrative text itself: any "sold
    [on] <weekday>" mention naming a day LATER in the week than
    report_date's own weekday shifts the effective date to that later day.

    Validated empirically before trusting this broadly: checked El Reno's
    own narrative across 9 weeks (this pattern correctly fired on exactly
    the 1 week it should have, not the other 8) and spot-checked ~15 other
    varied locations across ~90 reports with zero false positives -- "sold"
    + a weekday name earlier in (or equal to) the week never triggers,
    only an explicit later-day mention does.
    """
    if not narrative:
        return report_date
    own_wd = report_date.weekday()
    mentions = _SOLD_ON_RE.findall(narrative)
    later = [m for m in mentions if _WEEKDAYS.index(m) > own_wd]
    if not later:
        return report_date
    latest_wd = max(_WEEKDAYS.index(m) for m in later)
    return report_date + timedelta(days=latest_wd - own_wd)


def get_auth():
    key = os.environ.get("MARS_API_KEY")
    if not key:
        raise SystemExit("Set MARS_API_KEY in the environment before running.")
    return (key, "")


def init_db(conn):
    # Schema lives in snowflake/01_schema.sql + a one-time migration, not
    # provisioned by the app at runtime -- same convention as
    # basis-tracker-streamlit's database.py.
    if db.use_snowflake():
        return
    # WAL mode lets other processes (Streamlit, backfill_ftp.py) keep
    # reading the DB while this write transaction is open.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mars_sales (
            report_date TEXT NOT NULL,
            raw_date TEXT NOT NULL,
            slug_id INTEGER NOT NULL,
            location TEXT NOT NULL,
            state TEXT NOT NULL,
            weight_low INTEGER NOT NULL,
            muscle_grade TEXT NOT NULL,
            head_count INTEGER NOT NULL,
            avg_weight REAL NOT NULL,
            avg_price REAL NOT NULL,
            -- Date the source report was PUBLISHED, when that can lag the sale
            -- (video/internet auctions). NULL for auction and direct rows,
            -- which publish on their own report date. See the gate in
            -- recompute_fci_daily().
            published_date TEXT,
            PRIMARY KEY (report_date, slug_id, weight_low, muscle_grade, avg_price, head_count)
        )
    """)
    # Migration for DBs created before raw_date existed.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(mars_sales)").fetchall()}
    if "raw_date" not in cols:
        conn.execute("ALTER TABLE mars_sales ADD COLUMN raw_date TEXT")
        conn.execute("UPDATE mars_sales SET raw_date = report_date WHERE raw_date IS NULL")
    # Migration for DBs created before published_date existed. Left NULL --
    # backfilling it would mean re-fetching PDFs AMS has already overwritten,
    # and NULL is the correct "available on its sale date" default for every
    # auction and direct row anyway.
    if "published_date" not in cols:
        conn.execute("ALTER TABLE mars_sales ADD COLUMN published_date TEXT")
    # Competitors' published FCI estimates, hand-entered from their daily
    # sheets (see add_peer_estimate.py). Kept in its own table rather than
    # alongside ours because these are third-party numbers with no head count,
    # weight or constituent detail behind them -- only a single figure per day.
    #
    # index_date is CME's index date, i.e. what their sheet is estimating, not
    # the date the sheet was issued. CIH heads its sheet with the index date;
    # Compass heads its with the issue date and names the index date in the
    # body ("Tuesday, September 8, 2026"), so read Compass carefully.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS peer_estimates (
            index_date TEXT NOT NULL,
            source TEXT NOT NULL,
            fci_value REAL NOT NULL,
            note TEXT,
            PRIMARY KEY (index_date, source)
        )
    """)
    # Our estimate as it stood at each run, frozen. fci_daily keeps only the
    # LATEST value per date, so without this our number quietly improves as
    # late auctions land while competitors' stay fixed at what they printed --
    # see snapshots.py for the measured size of that (worth +0.33 on 09/08).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fci_snapshots (
            index_date TEXT NOT NULL,
            run_date TEXT NOT NULL,
            run_slot TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            fci_value REAL NOT NULL,
            total_head INTEGER,
            n_locations INTEGER,
            PRIMARY KEY (index_date, run_date, run_slot)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fci_daily (
            report_date TEXT PRIMARY KEY,
            fci_value REAL NOT NULL,
            n_locations INTEGER NOT NULL,
            total_head INTEGER NOT NULL,
            same_day_price REAL,
            same_day_head INTEGER,
            same_day_avg_weight REAL
        )
    """)
    conn.commit()


def fetch_slug_payload(slug_id, since_str, until_str, auth):
    """
    The WHOLE response for one slug -- `results` AND the `stats` block beside
    it.

    stats carries returnedRows / totalRows (observed on slug 1827 today:
    {"returnedRows": 178, "userAllowedRows": 100000, "totalRows": 178}), and
    that equality is the only signal AMS gives that a payload is complete.
    mars_census needs it: HTTP 200 with well-formed JSON and fewer rows than
    the report holds is indistinguishable from a withdrawal, and would present
    as an entire sale day going phantom. Nothing else in the ingest reads it.
    """
    resp = requests.get(
        f"{MARS_BASE}/reports/{slug_id}",
        auth=auth,
        params={"q": f"report_begin_date={since_str}:{until_str}"},
        timeout=(5, 60),
    )
    resp.raise_for_status()
    return resp.json()


def fetch_slug(slug_id, since_str, until_str, auth):
    """
    Just the rows. KEEPS ITS SIGNATURE AND RETURN SHAPE: calf_sales.py calls
    this as ui.fetch_slug(), and the census is not a reason to make the cash
    ingest learn about a stats block it has no use for.
    """
    return fetch_slug_payload(slug_id, since_str, until_str, auth).get("results", [])


def qualifying_rows(rows):
    """
    CME Rule 10203.A.1's sample: 700-899 lb Medium and Large Frame #1 and #1-2
    feeder STEERS, from a final (not preliminary) report.

    DO NOT ADD A lot_desc EXCLUSION HERE. Cattle reported as fancy, thin,
    fleshy, gaunt or full USED to be excluded, and CME's own explainer PDF
    "Understanding The CME Feeder Cattle Index" still says so -- but the rule
    was amended by SER-8154 (notice 22 May 2018, effective with the May 2019
    contract): "Rule 10203.A.1. shall no longer exclude cattle identified on
    USDA-AMS reports as being fancy, thin, fleshy, gaunt or full." The current
    rule text carries an orphan semicolon where that clause was cut out.

    This was nearly "fixed" on 2026-09-14 after a 297-head Ericson lot marked
    Fancy at $398.00/cwt moved the estimate $1.62. Excluding it was tested and
    REFUTED against live data: 15 other flagged lots (Unweaned, Fleshy, Thin
    Fleshed, Full) sit in the 2026-09-11 window, and INCLUDING all of them is
    what reproduces the published 341.7073 -- CIH printed 341.71 for the same
    date. Excluding them gives 341.9256, off by 22 cents. The empirical result
    and the rule agree; a stale PDF is what disagrees.

    The dairy/exotic/Brahma-breeding exclusion DID survive SER-8154, and is
    satisfied here by construction rather than by a filter of its own: AMS
    reports those cattle under their own class values, so the exact
    class == "Steers" match already drops them. Measured over 12 auctions,
    2026-08-20..09-14: Steers 829 rows, Dairy Steers 15, Beef/Dairy Steers 15.
    There is no breed field on these rows at all -- the only descriptor fields
    AMS carries are market_type, market_type_category, category and lot_desc,
    and across 641 qualifying rows none contained dairy, Holstein, Brahma or
    exotic wording. So loosening the class match to something like
    class.endswith("Steers") would quietly pull dairy cattle into the index.
    """
    out = []
    for r in rows:
        if (r.get("class") == "Steers"
                and r.get("frame") == "Medium and Large"
                and r.get("muscle_grade") in TARGET_GRADES
                and r.get("weight_break_low") in TARGET_BRACKETS
                and r.get("final_ind") == "Final"
                and r.get("head_count") and r.get("avg_weight") and r.get("avg_price")):
            out.append(r)
    return out


def mdY(d: date) -> str:
    return d.strftime("%m/%d/%Y")


def shift_weekend_to_monday(d: date) -> date:
    """
    CME's own methodology: Saturday and Sunday sales are treated as the
    following Monday's transactions for the rolling 7-day window (confirmed
    against CME's published rules). A handful of our roster locations
    genuinely sell on Saturday (Ericson NE is a fixed Saturday auction; a
    few others show up as occasional Saturday makeup sales), so this isn't
    a hypothetical edge case -- without it, those sales fall in the wrong
    week's window entirely. Weekday dates pass through unchanged.
    """
    if d.weekday() == 5:  # Saturday
        return d + timedelta(days=2)
    if d.weekday() == 6:  # Sunday
        return d + timedelta(days=1)
    return d


def derived_dates(row) -> tuple[str, str]:
    """
    The (report_date, raw_date) one served AMS row is STORED under, as ISO
    strings. Lifted verbatim out of run_update()'s roster loop below, which now
    calls it, so there is exactly ONE derivation of these two dates in the
    repository.

    That single copy is the whole point. mars_census.py has to key a served row
    the way the insert path keys it, and a second composition of
    detect_final_sale_day() + shift_weekend_to_monday() would agree on the day
    it was written and drift the first time either rule moved. The symptom of
    that drift is not a wrong date somewhere quiet -- it is the census
    reporting a whole sale day as withdrawn, which is exactly the false
    positive that would teach everyone to stop reading it.

    USDA's own report_date can understate a multi-day sale's true final day.
    Confirmed on a real report (El Reno OK, 9/1/26) where report_begin_date and
    report_end_date both said "09/01" (Tuesday) despite the narrative
    describing sales on Tuesday AND Wednesday -- so the correction to the real
    final day happens here, before any weekend shift, because raw_date's whole
    purpose is the TRUE calendar date rather than USDA's own label.

    report_date then carries CME's weekend shift on top of that: an Ericson NE
    (slug 1853) Saturday sale is filed under the following Monday. Both are
    returned because the two are used for different things -- the stored
    primary key is keyed on report_date, while a sale day is grouped by
    raw_date.
    """
    m, d, y = row["report_date"].split("/")          # MM/DD/YYYY
    sale_date = detect_final_sale_day(date(int(y), int(m), int(d)),
                                      row.get("report_narrative"))
    return shift_weekend_to_monday(sale_date).isoformat(), sale_date.isoformat()


def recompute_fci_daily(conn):
    """
    Recomputes the FULL fci_daily table from ALL stored mars_sales, using a
    rolling 7-calendar-day trailing window per CME's published methodology
    (each date's window can reach up to 6 days before any given `since`, so
    this is NOT limited to a recently-affected date range -- it's cheap
    given table size). Returns (n_written, first_date, last_date | None).
    """
    all_rows = conn.cursor().execute(
        "SELECT report_date, raw_date, head_count, avg_weight, avg_price, published_date, "
        "location FROM mars_sales ORDER BY report_date"
    ).fetchall()
    all_rows = [db.iso_row(r) for r in all_rows]

    # by_day keys off the (possibly weekend-shifted) report_date -- used for
    # BOTH the rolling 7-day window and the same-day snapshot below.
    #
    # An earlier version of this function used raw_date (the true calendar
    # sale date) for the same-day snapshot instead, on the theory that a
    # Saturday-only auction (e.g. Ericson NE) shouldn't leak into the
    # following Monday's "Daily" figure. That was wrong -- confirmed
    # directly against CME's own official daily FTP files (see cme_ftp.py):
    # a Monday file's own DAILY TOTALS line consistently equals that
    # Monday's own rows PLUS the preceding Saturday's, matching CME's stated
    # rule ("Saturday and Sunday sales... as if... occurred on Monday")
    # literally rather than just for the rolling window. raw_date is still
    # tracked and used for per-row display (e.g. the Sale Locations table,
    # cme_ftp_locations) -- CME's own files likewise keep a weekend row's
    # true date visible per-location while still folding its total into the
    # following business day's combined figure.
    #
    # A publication-date GATE was tried here on 2026-09-08 and REVERTED on
    # 2026-09-09. Do not reintroduce it without reading this.
    #
    # The idea: a video sale is reported under its final sale day, but AMS may
    # not publish it until the next business day, so it should arguably not
    # count toward an index date preceding its publication. It appeared to be
    # confirmed -- it fitted CME's then-current 9/3/2026 print of 328.80 to
    # within 0.06, where the ungated figure was 0.79 out.
    #
    # That print was PRELIMINARY. CME later revised 9/3 to 329.53 on 13,485
    # head -- up by exactly 1,376 head, which is precisely the Superior Labor
    # Day video volume (116 North Central + 1,260 South Central). So CME does
    # attribute a video sale to its SALE date. Its first print for a date
    # simply omits reports that have not landed yet, and a revision adds them.
    # Measured against the REVISED value, ungated is +0.06 and gated is -0.67.
    #
    # The lesson generalises: this reconstruction should be expected to track
    # CME's FINAL value for a date, and to differ from CME's FIRST print by
    # whatever had not yet been reported when CME computed it. Validating
    # against a fresh CME print therefore risks fitting a provisional number.
    #
    # published_date is still recorded on mars_sales (see run_update) -- the
    # lag is genuinely useful, since it explains why a first print and a final
    # print differ -- but it must NOT filter the window.
    by_day = {}  # report_date -> list of (weight_lbs, dollars, head)
    for report_date, raw_date, head, wt, price, published_date, location in all_rows:
        # Bucket the way CME buckets -- see LOCATION_BUCKET_SHIFT_DAYS.
        report_date = shifted_bucket_date(location, report_date)
        w = head * wt
        by_day.setdefault(report_date, []).append((w, w * price, head))

    all_dates = sorted(date.fromisoformat(d) for d in by_day)
    if not all_dates:
        db.truncate(conn, "fci_daily")
        conn.commit()
        return 0, None, None
    first_date, last_date = all_dates[0], all_dates[-1]
    db.truncate(conn, "fci_daily")

    n_written = 0
    d = first_date
    while d <= last_date:
        window_start = d - timedelta(days=6)
        window_days = [
            (window_start + timedelta(days=i)).isoformat() for i in range(7)
        ]
        den = num = 0.0
        n_locs = 0
        total_head = 0
        d_iso = d.isoformat()
        for wd in window_days:
            for w, dollars, head in by_day.get(wd, []):
                den += w
                num += dollars
                n_locs += 1
                total_head += head

        # Same-day-only snapshot (not rolling) -- matches the "Daily: $X on Y
        # head and Z lbs average" figure CME's own subscriber reports quote
        # alongside the 7-day index. None when no report landed that date
        # (weekends etc.), same as the report showing no standalone row then.
        sd_den = sd_num = 0.0
        sd_head = 0
        for w, dollars, head in by_day.get(d_iso, []):
            sd_den += w
            sd_num += dollars
            sd_head += head
        sd_price = (sd_num / sd_den) if sd_den > 0 else None
        sd_avg_weight = (sd_den / sd_head) if sd_head > 0 else None

        if den > 0:
            conn.cursor().execute(
                "INSERT INTO fci_daily "
                "(report_date, fci_value, n_locations, total_head, same_day_price, same_day_head, same_day_avg_weight) "
                f"VALUES ({db.placeholders(7)})",
                (d.isoformat(), num / den, n_locs, total_head, sd_price, sd_head or None, sd_avg_weight),
            )
            n_written += 1
        d += timedelta(days=1)
    conn.commit()
    return n_written, first_date, last_date


def run_update(since: date, verbose=True):
    auth = get_auth()
    roster = json.loads(ROSTER_PATH.read_text(encoding="utf-8"))
    until = date.today()
    since_str, until_str = mdY(since), mdY(until)

    conn = db.get_conn()
    init_db(conn)

    total_inserted = 0
    # Kept for the census at the end of the run, and the reason it costs ZERO
    # extra HTTP: this loop already holds every roster slug's raw payload, and
    # the census's whole question is about those same rows. Two dict
    # assignments on objects resp.json() has already materialised -- no
    # derivation, no keying and no database inside this loop -- so the entire
    # blast radius of the census stays inside the one guarded call at the end.
    #
    # The retention is real and named in case it ever matters: ~75-300 raw rows
    # per slug across 89 slugs, tens of MB, alive for the 15-20 minutes this
    # function runs. If it ever does matter, keep the keyed sets per slug
    # instead of the rows, at the cost of moving derivation back in here.
    #
    # census_roster IS THE ROSTER, whatever the fetches do. It is what the run
    # SET OUT to fetch, and the census needs it to tell "checked and clean"
    # from "never checked": a slug missing from census_payloads is a slug
    # nobody looked at, and without the roster to compare against there is
    # nothing left in the run that remembers it should have been.
    census_payloads, census_locations = {}, {}
    census_roster = [int(loc["slug_id"]) for loc in roster]
    for loc in roster:
        slug_id = loc["slug_id"]
        census_locations[int(slug_id)] = loc["city"] or loc["title"]
        try:
            payload = fetch_slug_payload(slug_id, since_str, until_str, auth)
        except Exception as e:
            if verbose:
                print(f"  [skip] slug {slug_id} ({loc['title']}): {e}")
            # NOT a key in census_payloads, so no group of this slug is ever
            # JUDGED -- a slug we failed to fetch must never be judged absent,
            # and that is structure rather than a guard. It stays on
            # census_roster, though, so the census reports it as WITHHELD
            # rather than passing over it in silence.
            continue
        rows = payload.get("results", [])
        census_payloads[slug_id] = payload
        qrows = qualifying_rows(rows)
        for r in qrows:
            # One derivation for the insert path and the census both -- see
            # derived_dates() for why a second copy would be a false-positive
            # generator rather than a tidy-up.
            iso_date, raw_iso = derived_dates(r)
            cols = ["report_date", "raw_date", "slug_id", "location", "state",
                    "weight_low", "muscle_grade", "head_count", "avg_weight", "avg_price"]
            values = (iso_date, raw_iso, slug_id, loc["city"] or loc["title"], loc["state"],
                      r["weight_break_low"], r["muscle_grade"],
                      r["head_count"], r["avg_weight"], r["avg_price"])
            db.merge_ignore(conn, "mars_sales", cols, values,
                             ["report_date", "slug_id", "weight_low", "muscle_grade", "avg_price", "head_count"])
        total_inserted += len(qrows)
        if verbose and qrows:
            print(f"  {loc['state']:>2} {loc['city'] or loc['title']:<28} +{len(qrows)} rows")

    # Direct/Video/Internet trade (Direct Cattle Report family). These PDFs
    # always show the CURRENT week only -- there's no historical-date param,
    # so this only extends the dataset forward from whenever it's first run,
    # same limitation the original auction backfill had. Weekly, not daily:
    # every qualifying row gets that week's Friday date (CME's own rule
    # treats direct-trade reports as Friday sales).
    if verbose:
        print("\nDirect trade reports (this week only):")
    direct_results = fetch_all_direct_rows(verbose=verbose)
    direct_inserted = 0
    direct_unlabelled = []
    for state, (report_date_, rows, unlabelled_) in direct_results.items():
        for _u in unlabelled_:
            direct_unlabelled.append((state, report_date_, _u))
        if report_date_ is None:
            continue
        iso_date = report_date_.isoformat()
        cols = ["report_date", "raw_date", "slug_id", "location", "state",
                "weight_low", "muscle_grade", "head_count", "avg_weight", "avg_price"]
        key_cols = ["report_date", "slug_id", "weight_low", "muscle_grade", "avg_price", "head_count"]
        for r in rows:
            values = (iso_date, iso_date, DIRECT_REPORT_SLUGS[state], f"{state} DIRECT", state,
                      r["weight_break_low"], r["muscle_grade"],
                      r["head_count"], r["avg_weight"], r["avg_price"])
            db.merge_ignore(conn, "mars_sales", cols, values, key_cols)
        direct_inserted += len(rows)
    total_inserted += direct_inserted
    # Surfaced AFTER the loop so it is the last thing on screen for this stage
    # rather than buried among ten states' progress lines. Empty on an ordinary
    # run; see parse_direct_pdf's docstring for why it has no threshold.
    if direct_unlabelled and verbose:
        print("\n  *** {} DIRECT ROW(S) IN THE INDEX WEIGHT BAND WERE NOT "
              "INGESTED because this parser could not resolve a "
              "Delivery/Freight label. This is a PARSE FAILURE, not an "
              "exclusion -- the index is short by this much:".format(
                  len(direct_unlabelled)))
        for st, rd, uu in direct_unlabelled:
            print("        {} DIRECT {}  {:,} head at {:.0f} lb, ${:.2f}, "
                  "grade {}".format(st, rd, uu["head_count"], uu["avg_weight"],
                                    uu["avg_price"], uu["muscle_grade"]))

    # Video/internet auction trade: Superior Livestock (by far the largest
    # platform, ~200k head/week), plus Cattle Country Video, CMS, LiveAg,
    # and Northern Livestock (see video_reports.py for the smaller per-city
    # add-ons checked and skipped as not worth building). Same
    # current-week-only limitation as the direct reports. Rows are
    # attributed to a REGION (North Central /
    # South Central), not a single state -- video sales aren't broken out
    # by state within a region -- so `state` here is the region name
    # itself, not a real 2-letter code; that's intentional, not a bug.
    if verbose:
        print("\nVideo auction reports (this week only):")
    video_results = fetch_all_video_rows(verbose=verbose)
    video_inserted = 0
    video_stale = 0
    for name, (report_date_, published_date_, rows) in video_results.items():
        if report_date_ is None:
            continue
        # AMS keeps the last edition of a seasonal report posted forever, so
        # a successful fetch is NOT evidence of a recent sale. See
        # VIDEO_MAX_AGE_DAYS in video_reports.py for what this prevents.
        age_days = (date.today() - report_date_).days
        if age_days > VIDEO_MAX_AGE_DAYS:
            video_stale += 1
            if verbose:
                print(f"  [stale] {name} VIDEO {report_date_} is {age_days}d "
                      f"old -- skipped (AMS still serves the last edition)")
            continue
        iso_date = shift_weekend_to_monday(report_date_).isoformat()
        # Gate the index on the LATER of the two: a sale shifted off a weekend
        # can't become available before its report was actually published.
        pub_iso = (
            max(published_date_.isoformat(), iso_date) if published_date_ else None
        )
        slug_id = VIDEO_REPORT_SLUGS[name]
        cols = ["report_date", "raw_date", "slug_id", "location", "state",
                "weight_low", "muscle_grade", "head_count", "avg_weight", "avg_price",
                "published_date"]
        key_cols = ["report_date", "slug_id", "weight_low", "muscle_grade", "avg_price", "head_count"]
        for r in rows:
            values = (iso_date, report_date_.isoformat(), slug_id, f"{name} VIDEO ({r['region']})", r["region"],
                      r["weight_break_low"], r["muscle_grade"],
                      r["head_count"], r["avg_weight"], r["avg_price"], pub_iso)
            db.merge_ignore(conn, "mars_sales", cols, values, key_cols)
        # merge_ignore leaves an existing row untouched, so video rows stored
        # before published_date existed keep a NULL and would slip past the
        # gate in recompute_fci_daily(). Stamp them from this run's header.
        if pub_iso:
            ph = db.placeholders(1)
            conn.cursor().execute(
                f"UPDATE mars_sales SET published_date={ph} "
                f"WHERE report_date={ph} AND slug_id={ph} AND published_date IS NULL",
                (pub_iso, iso_date, slug_id),
            )
        video_inserted += len(rows)
    total_inserted += video_inserted
    conn.commit()

    # Verify the per-location bucketing corrections still describe CME's own
    # files. These are observed patterns rather than published rules, so they
    # can rot silently; this makes that loud instead. Warnings only -- a
    # drifted assumption should not abort the day's refresh.
    for _w in check_bucket_drift(conn):
        print(f"  [!] {_w}")

    n_written, first_date, last_date = recompute_fci_daily(conn)

    # Freeze this run's estimates before anything can revise them. Must come
    # after the recompute and before the process exits, or the morning call is
    # lost for good -- fci_daily is overwritten wholesale by the next run.
    n_frozen = capture_snapshots(conn)

    if verbose:
        print(f"\nInserted/kept {total_inserted} sale rows: {total_inserted - direct_inserted - video_inserted} "
              f"auction rows across {len(roster)} locations, {direct_inserted} direct-trade rows across "
              f"{len(direct_results)} states, {video_inserted} video-auction rows across {len(video_results)} reports.")
        print(f"Recomputed FCI (7-day rolling window) for {n_written} dates "
              f"({first_date or '—'} to {last_date or '—'}).")
        print(f"Froze {n_frozen} new estimate snapshot(s) for this run's slot "
              f"(0 is normal for a repeat run in the same slot).")
        recent = conn.cursor().execute(
            "SELECT report_date, fci_value, n_locations FROM fci_daily ORDER BY report_date DESC LIMIT 8"
        ).fetchall()
        print("\nMost recent reconstructed index values:")
        for d, v, n in recent:
            print(f"  {db.iso(d)}  ${v:.2f}   ({n} locations)")

        # The barn report does NOT run here -- see print_barn_report() below for
        # why it cannot, and scripts/daily_update.ps1 for where it does.

    # The census DOES run here, and unconditionally rather than under
    # `verbose`: the log is the standalone's only output and costs nothing,
    # and the write below is what the dashboard reads. See write_census().
    write_census(census_payloads, since, until, census_locations, census_roster)
    conn.close()


# A bound on WAITING, not on reporting. It cannot change a single finding --
# only how long a finished index waits for a diagnostic before going to the
# push without it -- which is why it is here, at the call site, and not a
# threshold inside a module whose whole argument is that it has none.
#
# Measured on the real payloads: the census takes about one second. 120 is two
# orders of magnitude of headroom, so it can only ever fire on something that
# is genuinely stuck.
CENSUS_DEADLINE_SECONDS = 120


def _census_worker(payloads, since, until, locations, roster, out):
    """
    The census, off the main thread, on its OWN connection.

    ITS OWN CONNECTION BECAUSE IT HAS TO BE. sqlite3 connections are
    check_same_thread=True, so run_update()'s connection cannot be touched
    from here; db.get_conn() is the shim's own constructor and works on both
    backends. run_update() has committed the index, the snapshots and every
    merge by the time this starts, so the two connections never contend for
    the write lock -- and if they somehow did, SQLite's busy timeout bounds it
    at five seconds and raises into the guard below.

    Nothing is printed from this thread. The lines go back to the caller and
    are printed there, so a census that overruns its deadline cannot scribble
    into the middle of whatever the run is doing by then.
    """
    try:
        import mars_census
        conn = db.get_conn()
        try:
            findings = mars_census.run_census(conn, payloads, since, until,
                                              locations, roster)
            out["lines"] = list(mars_census.report_lines(findings))
        finally:
            conn.close()
    except Exception as e:                      # noqa: BLE001 -- diagnostic only
        out["lines"] = [f"  [!] AMS census skipped: {type(e).__name__}: {e}"]


def write_census(payloads, since, until, locations, roster):
    """
    Which stored rows AMS no longer serves. REPORT ONLY -- see mars_census.py,
    which holds no delete path and writes only its own two tables.

    SAFE TO RUN HERE BECAUSE IT RUNS LAST. The index is computed, committed and
    snapshotted by the time this is called, so nothing below can move a number.
    The ordering is load-bearing in a second way too: because this runs AFTER
    the merge_ignore loop, "AMS serves it and we do not hold it" cannot mean
    "not yet inserted" -- every qualifying served row was already offered to
    merge_ignore, so it can only mean merge_ignore DECLINED it. Run before the
    loop, every new row of the day would read as missing.

    IT MUST NOT BE ABLE TO FAIL THE RUN, and this is barn_report's pattern
    including both lessons that shaped it:

      THE IMPORT IS INSIDE THE GUARD. `import mars_census` at module scope
      would let a syntax error in a report-only diagnostic take down the whole
      ingest before a single row was fetched -- which is precisely what
      happened when update_index.py imported barn_report at the top.

      THE CONSUMPTION IS INSIDE IT TOO. run_census() promises to return a
      Findings and report_lines() promises a materialised list of strings, but
      this is where those promises are CONSUMED. Iterating a result that came
      back None is what once exited 1 after the index was computed and before
      it was pushed. A diagnostic must never be what strands a finished index,
      so the guard goes round both sides.

      AND THE GUARD IS ROUND TIME AS WELL AS ROUND EXCEPTIONS, because a
      try/except cannot catch a hang and a hang here is worse than a crash.
      scripts/daily_update.ps1 pushes as a LATER STEP: it waits on this
      process with Start-Process -Wait, so a census that never returns means
      update_index.py never exits, the push never runs, and a perfectly good
      index sits in SQLite unpublished. That is the exact failure the step
      ordering in CLAUDE.md exists to prevent, and it was reproduced through
      this call site -- run_census() made to sleep, EXIT=124, the index
      recomputed for 950 dates and never pushed.

      WHY A DEADLINE AND NOT A BUSY TIMEOUT. The obvious candidate for a hang
      is the SQLite write blocking on a lock held by the dashboard or by an
      overlapping run. Measured: it does not hang. Python's sqlite3 opens with
      busy_timeout = 5000, and a held write lock raises OperationalError after
      5.5 seconds straight into the guard above, after which the run continues
      and the push happens. Bounding the write would therefore have been a fix
      for a failure that does not occur, while the one that does -- the call
      not returning at all, for any reason -- stayed wide open.

      WHY NOT ITS OWN STEP AFTER THE PUSH. That is how the barn report escaped
      an ordering problem, but the barn report reads only tables. The census's
      input is THIS PROCESS'S MEMORY: run_update()'s roster loop already holds
      every payload, which is why the census costs zero extra HTTP. A post-push
      step would have to re-walk all 89 slugs, putting the network back into a
      module built on not having any.

      So the census runs on a daemon thread and the main thread waits
      CENSUS_DEADLINE_SECONDS for it. An overrun is reported and abandoned;
      the thread is a daemon, so it cannot hold the process open either.

      WHAT AN ABANDONED CENSUS LEAVES BEHIND, measured rather than reasoned
      about, because the first version of this paragraph got it backwards. It
      claimed the truncate-then-insert order leaves mars_census_runs EMPTY and
      the panel therefore reads "unavailable". It does not. The abandoned
      thread never reaches conn.commit(), so SQLite rolls its whole
      transaction back and BOTH TABLES KEEP THE PREVIOUS RUN'S CONTENTS --
      verified by truncating, inserting, abandoning, and reading the file back
      from a new process.

      That is the same thing an abandoned census leaves as a census that
      RAISES, which is the already-accepted path above, so it adds no new
      failure shape. It is honest for one reason only: the panel's headline
      leads with the run_at of the run it is describing, so a statement from
      07:31 says 07:31, and mars_census_view adds "last ran N hours ago" past
      STALE_HOURS. It is NOT this run's all-clear and must never be described
      as one -- which is what the overrun line below says, and why it names
      the stamp rather than promising "unavailable".
    """
    print()
    out = {}
    worker = threading.Thread(target=_census_worker, name="ams-census",
                              daemon=True,
                              args=(payloads, since, until, locations, roster,
                                    out))
    worker.start()
    worker.join(CENSUS_DEADLINE_SECONDS)
    if worker.is_alive():
        print(f"  [!] AMS census did not finish within "
              f"{CENSUS_DEADLINE_SECONDS}s and was abandoned. The index is "
              f"computed and committed and this run continues to the push. "
              f"THIS RUN MADE NO STATEMENT ABOUT AMS: the abandoned write was "
              f"never committed, so the reconciliation panel still shows the "
              f"PREVIOUS run's result, stamped with that run's own time.")
        return
    for line in out.get("lines", ()):
        print(line)


def print_barn_report(conn):
    """
    Which barns the index date is still waiting on, and how big they are.

    THIS MUST RUN AFTER THE CME PULL, WHICH IS WHY IT IS NOT IN run_update().
    barn_report picks the index date it describes from MAX(cme_ftp_daily) -- the
    day after CME's last print is the day we are estimating -- and
    daily_update.ps1 pulls CME's file AFTER the ingest. Called from inside
    run_update() it therefore read a cme_ftp_daily one print stale and named
    YESTERDAY'S index date every single morning. On 2026-09-25 it printed
    "index date 2026-09-23 (Wed)" while the number being published, and the one
    the dashboard and the client estimate both led with, was 2026-09-24. The
    rule was right; only the ordering was wrong, and nothing noticed for two
    days because the report is correct-looking either way.

    Verified at the time: with cme_ftp_daily through 09-22 the rule gives 09-23,
    and through 09-23 it gives 09-24. The pull is what moves it.

    PRINT ONLY. The index is already computed, pushed and snapshotted by the
    time this runs; nothing here can change a number.

    THE ITERATION IS INSIDE THE GUARD, not just the call. report_lines()
    promises never to raise and to hand back a materialised list of strings,
    but this loop is where that promise is CONSUMED, and it used to sit outside
    any try of its own: stubbing the report to return None took the whole run
    to exit 1, after the index was computed and before it was pushed. A
    diagnostic must never be what strands a finished index, so the guard
    belongs on both sides.
    """
    print()
    try:
        # Imported HERE, not at module scope. While run_update() printed the
        # report, update_index.py imported barn_report at the top -- so a syntax
        # error or a bad import in a PRINT-ONLY diagnostic took down the whole
        # ingest before a single row was fetched. Nothing else in this file
        # needs it, so the blast radius of a broken report is now this function.
        import barn_report
        for line in barn_report.report_lines(conn):
            print(line)
    except Exception as e:                      # noqa: BLE001 -- diagnostic only
        print(f"  [!] barn report skipped: {type(e).__name__}: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", type=str, default=None,
                         help="ISO date to pull from (default: continue from last stored date, "
                              "or 2026-01-24 on first run)")
    parser.add_argument("--barn-report-only", action="store_true",
                         help="print the missing-barn report and exit, fetching and "
                              "computing nothing. The daily job runs this AFTER the CME "
                              "pull, because the report names the index date derived from "
                              "MAX(cme_ftp_daily) and inside the ingest that value is one "
                              "print stale.")
    args = parser.parse_args()

    if args.barn_report_only:
        conn = db.get_conn()
        try:
            print_barn_report(conn)
        finally:
            conn.close()
        raise SystemExit(0)

    if args.since:
        since = date.fromisoformat(args.since)
    elif db.use_snowflake() or DB_PATH.exists():
        conn = db.get_conn()
        row = conn.cursor().execute("SELECT MAX(report_date) FROM fci_daily").fetchone()
        conn.close()
        since = (date.fromisoformat(db.iso(row[0])) - timedelta(days=REFETCH_LOOKBACK_DAYS)
                 if row and row[0] else CONTINUATION_START)
    else:
        since = CONTINUATION_START

    print(f"Updating CME Feeder Cattle Index reconstruction since {since.isoformat()}...\n")
    run_update(since)
