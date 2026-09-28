"""
Heifers as a share of steer + heifer feeder auction receipts: the volume-side
read on whether producers are keeping females back.

WHY THIS BELONGS NEXT TO THE RETENTION RATIO. replacement_reports.py prices the
retention DECISION -- what a bred female fetches against her salvage value. This
measures the CONSEQUENCE: when producers keep heifers home to breed, those
heifers stop showing up at the feeder auction and the heifer share of receipts
falls. One is an incentive, the other is behaviour, and they can disagree --
which is the point of carrying both.

WHAT IS STORED, AND WHY IT IS AGGREGATED. One row per (week, report), holding
only the steer and heifer head counts. The line items behind those counts run to
roughly 15,000 rows per state per year and nothing downstream reads them, so
storing them would mean pushing millions of rows to Snowflake nightly to compute
two numbers. The aggregation is lossless for this purpose: a head count is a sum.

TWO SOURCES, ONE SERIES.

    2011-2019   USDA legacy livestock auction archive (source 'legacy')
    2019-now    AMS state weekly summaries via MARS  (source 'mars')

MARS simply does not go back: the state summaries begin in spring 2019, staggered
by state as offices came online (OK 04/08, KS 04/22, TX 05/06), and Q1 2019
returns nothing for any of them. Everything before that lives in a set of zip
archives at https://mymarketnews.ams.usda.gov/legacydata/lpgmn.

THE SPLICE WAS CHECKED, NOT ASSUMED:
  * identical definitions -- legacy CLASS_NAME "Feeder Steers"/"Feeder Heifers"
    against MARS commodity "Feeder Cattle" + class "Steers"/"Heifers". Both
    exclude feeder bulls and Holsteins, which the archive carries separately,
    and legacy SALE_TYPE is 100% "Auction", matching the panel's footprint.
  * same market -- legacy 2018 carried 4,343,715 head over the panel's weeks and
    states; MARS 2020 carried 4,415,818. A ratio of 0.984.
  * no step at the join -- 2018 45.77%, 2020 45.14%.

AND WHAT COULD NOT BE CHECKED. The two sources never overlap at full strength.
Legacy runs normally through 2019 week 17 (87k-222k head/week) and then collapses
as USDA retires it -- 41k, 27k, 17k, 6k -- while MARS does not complete its panel
until week 19. Comparing them week-by-week across that window reports a 3.96 pt
disagreement that is entirely an artefact of legacy's dying remnant, which skews
heifer-heavy at 51-60%. So 2019 is stored from BOTH sources and the analytics
splice it at week 17, flagged rather than presented as measured.

THE LEGACY ARCHIVE CANNOT BE FETCHED BY A SCRIPT. That file host refuses every
programmatic client -- python requests gets RemoteDisconnected, curl gets a
connection reset, .NET WebClient gets a WebException -- while serving a browser
normally. So --legacy takes a path to an already-downloaded zip rather than a
URL. This is not a limitation worth engineering around: the archive is static and
ends in 2020, so it is loaded once and never again.

    python feeder_sex_mix.py                      # MARS, last 120 days
    python feeder_sex_mix.py --since 2019-01-01   # MARS backfill
    python feeder_sex_mix.py --legacy path/to/usda_legacy_ls_auction_wtd_2_2010_2019.zip
    python feeder_sex_mix.py --show
"""
import argparse
import csv
import io
import zipfile
from collections import defaultdict
from datetime import date, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

import snowflake_db as db

MARS_BASE = "https://marsapi.ams.usda.gov/services/v1.2"

# The panel: every AMS state weekly summary carrying feeder receipts in all of
# 2020-2026 with at least 25 weeks of them each year. The threshold drops
# reports whose feeder coverage collapsed (Pennsylvania: 37 weeks in 2024 to 1
# in 2026) or halved (Colorado: 36 to 13), either of which would move the share
# for a reporting reason rather than a market one. It is not a delicate choice:
# thresholds of 0, 20, 25 and 30 weeks all give the same series within 0.2 pts.
#
# MISSOURI IS THE TRAP. It publishes a statewide summary AND seven regional ones
# that PARTITION it -- the regionals summed to 741,620 head in 2026 against the
# statewide's 748,899. Counting both would double the panel's largest state.
# Only 1821 is listed here, and the regionals (1785, 1837, 1838, 1841, 1842,
# 1845, 1847) must never be added.
#
# Virginia legitimately contributes twice: 2187 is "Auction Livestock (Special
# Graded)" and 2148 is "Auction Livestock (Board Sale)" -- disjoint market
# types, verified, so they are separate channels rather than a double count.
PANEL_SLUGS = {
    1704: ("FL", "Florida Weekly Livestock Auction Summary"),
    1778: ("MT", "Montana Weekly Livestock Auction Summary"),
    1784: ("NM", "New Mexico Weekly Cattle Summary"),
    1821: ("MO", "Missouri Weekly Cattle Auction Summary"),
    1831: ("OK", "Oklahoma Weekly Cattle Auction Summary"),
    1860: ("NE", "Nebraska Weekly Livestock Auction Summary"),
    1895: ("KS", "Kansas Weekly Cattle Auction Summary"),
    1933: ("GA", "Georgia Weekly Livestock Auction Summary"),
    1955: ("TX", "Texas Weekly Cattle Auction Summary"),
    1963: ("SC", "South Carolina Weekly Livestock Auction Summary"),
    2006: ("AL", "Alabama Weekly Cattle Auction Summary"),
    2027: ("SD", "South Dakota Weekly Cattle Auction Summary"),
    2056: ("AR", "Arkansas Weekly Livestock Auction Summary"),
    2063: ("TN", "Tennessee Weekly Cattle Auction Summary"),
    2091: ("NC", "North Carolina Weekly Livestock Auction Summary"),
    2106: ("WY", "Wyoming Weekly Cattle Auction Summary"),
    2115: ("MS", "Mississippi Weekly Livestock Auction Summary"),
    2148: ("VA", "Virginia Weekly Feeder Cattle Board Sale Summary"),
    2167: ("IA", "Iowa Weekly Cattle Auction Summary"),
    2187: ("VA", "Virginia Weekly Cattle Auction Summary"),
    2193: ("KY", "Kentucky Weekly Livestock Auction Summary"),
}

# MARS calls it commodity + class; the legacy archive folds both into CLASS_NAME.
MARS_COMMODITY = "Feeder Cattle"
MARS_CLASSES = {"Steers": "steers", "Heifers": "heifers"}
LEGACY_CLASSES = {"Feeder Steers": "steers", "Feeder Heifers": "heifers"}

# slug_id for legacy rows, which have no MARS report behind them. A sentinel
# rather than NULL so the natural key stays usable on both backends.
LEGACY_SLUG = 0

COLUMNS = ["week_start", "source", "slug_id", "state", "steers", "heifers"]


def init_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS feeder_receipts (
            week_start TEXT NOT NULL,
            source TEXT NOT NULL,
            slug_id INTEGER NOT NULL,
            state TEXT,
            steers INTEGER NOT NULL,
            heifers INTEGER NOT NULL,
            PRIMARY KEY (week_start, source, slug_id, state)
        )
    """)
    conn.commit()


def get_auth():
    import os
    return (os.environ["MARS_API_KEY"], "")


def _week_start(d: date) -> str:
    """Monday of the ISO week holding `d`, as an ISO date string."""
    iso = d.isocalendar()
    return date.fromisocalendar(iso[0], iso[1], 1).isoformat()


def _mdy(s):
    m, d, y = str(s).split("/")
    return date(int(y), int(m), int(d))


def _upsert(conn, rows, source):
    """Replace whole (week, source, slug) groups rather than updating in place.

    A re-run or an AMS correction must not layer a second copy on top of the
    first, and these are sums -- a partial overwrite would silently double a
    week's head count.
    """
    ph = db.placeholders(len(COLUMNS))
    n = 0
    for (week, slug_id, state), (s, h) in sorted(rows.items()):
        conn.cursor().execute(
            f"DELETE FROM feeder_receipts WHERE week_start = {db.placeholders(1)} "
            f"AND source = {db.placeholders(1)} AND slug_id = {db.placeholders(1)} "
            f"AND state = {db.placeholders(1)}", (week, source, slug_id, state))
        conn.cursor().execute(
            f"INSERT INTO feeder_receipts ({','.join(COLUMNS)}) VALUES ({ph})",
            (week, source, slug_id, state, s, h))
        n += 1
    conn.commit()
    return n


def fetch_slug(slug_id, since, until, auth):
    r = requests.get(f"{MARS_BASE}/reports/{slug_id}", auth=auth,
                     params={"q": f"report_begin_date={since}:{until}"},
                     timeout=(5, 300))
    r.raise_for_status()
    return r.json().get("results", [])


def ingest_mars(conn, since: date, until: date, verbose=True):
    """Fetch the panel over a window and store weekly steer/heifer head."""
    auth = get_auth()
    s, u = since.isoformat(), until.isoformat()
    agg = defaultdict(lambda: [0, 0])
    n_reports = 0

    for slug_id, (state, name) in sorted(PANEL_SLUGS.items()):
        try:
            rows = fetch_slug(slug_id, s, u, auth)
        except Exception as e:
            if verbose:
                print(f"  [!] {slug_id} {name[:40]:<40} {type(e).__name__}: {e}")
            continue
        kept = 0
        for x in rows:
            if x.get("commodity") != MARS_COMMODITY:
                continue
            idx = MARS_CLASSES.get(x.get("class"))
            if not idx or not x.get("head_count"):
                continue
            # report_begin_date is the week the trade happened; report_date is
            # when AMS published it, and for a weekly summary those differ.
            raw = x.get("report_begin_date") or x.get("report_date")
            if not raw:
                continue
            key = (_week_start(_mdy(raw)), slug_id, state)
            agg[key][0 if idx == "steers" else 1] += int(x["head_count"])
            kept += 1
        if kept:
            n_reports += 1
        if verbose:
            print(f"  {name[:44]:<44} {kept:>6} feeder rows")

    n = _upsert(conn, agg, "mars")
    return n_reports, n


def load_legacy(conn, zip_path, verbose=True):
    """Load the pre-2019 archive from an already-downloaded zip.

    Only the states the MARS panel covers are kept, so the two eras describe the
    same footprint and the series does not gain territory at the seam.
    """
    panel_states = {st for st, _ in PANEL_SLUGS.values()}
    agg = defaultdict(lambda: [0, 0])
    zf = zipfile.ZipFile(zip_path)
    for name in sorted(zf.namelist()):
        if not name.lower().endswith(".csv"):
            continue
        kept = 0
        with zf.open(name) as fh:
            stream = io.TextIOWrapper(fh, encoding="utf-8", errors="replace", newline="")
            for r in csv.DictReader(stream):
                idx = LEGACY_CLASSES.get(r.get("CLASS_NAME"))
                if not idx:
                    continue
                st = (r.get("STATE_ABBREV") or "").strip()
                if st not in panel_states:
                    continue
                hc = r.get("HEAD_COUNT")
                if not hc:
                    continue
                try:
                    hc = int(float(hc))
                    d = _mdy(r["LGDATE"])
                except (ValueError, KeyError):
                    continue
                if hc <= 0:
                    continue
                agg[(_week_start(d), LEGACY_SLUG, st)][0 if idx == "steers" else 1] += hc
                kept += 1
        if verbose:
            print(f"  {name[:50]:<50} {kept:>8,} feeder rows")
    n = _upsert(conn, agg, "legacy")
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None, help="ISO date (default: 120 days back)")
    ap.add_argument("--until", default=None, help="ISO date (default: today)")
    ap.add_argument("--legacy", default=None,
                    help="path to a downloaded usda_legacy_ls_auction_*.zip")
    ap.add_argument("--show", action="store_true", help="print the series and exit")
    a = ap.parse_args()

    conn = db.get_conn()
    init_tables(conn)

    if a.show:
        rows = conn.cursor().execute(
            "SELECT week_start, source, steers, heifers FROM feeder_receipts").fetchall()
        by_year = defaultdict(lambda: [0, 0])
        for ws, src, s, h in rows:
            iso = date.fromisoformat(str(db.iso(ws))).isocalendar()
            if iso[1] > 37:
                continue
            by_year[iso[0]][0] += s
            by_year[iso[0]][1] += h
        print(f"{'year':<6}{'steers':>12}{'heifers':>12}{'share':>9}   (YTD through week 37)")
        for y in sorted(by_year):
            s, h = by_year[y]
            if s + h:
                print(f"{y:<6}{s:>12,}{h:>12,}{100 * h / (s + h):>8.2f}%")
        conn.close()
        return

    if a.legacy:
        print(f"loading legacy archive {a.legacy}")
        n = load_legacy(conn, a.legacy)
        print(f"stored {n:,} week/state rows")
    else:
        until = date.fromisoformat(a.until) if a.until else date.today()
        since = date.fromisoformat(a.since) if a.since else until - timedelta(days=120)
        print(f"fetching {len(PANEL_SLUGS)} reports, {since} .. {until}")
        n_reports, n = ingest_mars(conn, since, until)
        print(f"{n_reports} report(s), stored {n:,} week/report rows")
    conn.close()


if __name__ == "__main__":
    main()
