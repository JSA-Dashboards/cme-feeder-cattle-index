"""
AMS US/Mexico border cattle reports: trade status and market commentary.

WHAT THIS SOURCE DOES AND DOES NOT GIVE. Every one of AMS's seven
International Livestock reports returns ZERO structured data fields through the
MARS API -- no head_count, no avg_price, no volumes. Verified 2026-09-10 across
all seven. The import tables exist only in the published report body on
mymarketnews, not in the API. So this module deliberately stores TEXT, and the
volume series has to come from Census trade statistics instead.

What it does give is worth having:

  special_notes (3629)  the official trade status, weekly and machine-readable.
                        Currently "*** EXPORTS TO MEXICO REMAIN SUSPENDED UNTIL
                        FURTHER NOTICE. ***". For a border page, "is the border
                        open" is the single most important field, and MARS
                        carries it reliably even though it carries no numbers.

  report_narrative      port-level market colour: which crossing, trade tone,
  (3486)                weight ranges. "Douglas, AZ - Compared to Tuesday,
                        steer calves and yearlings sold steady. Heifers not
                        tested. Trade active, demand good. Supply consisted of
                        steers weighing 500-800 lbs."

AND THE CROSSING-DAY COUNT IS A VOLUME PROXY -- BUT NOT THE REPORT COUNT.

The tempting version of this is "AMS publishes only when cattle cross, so count
the reports". Measured, that is wrong in the one year it matters: since 2026 AMS
also publishes on days when NOTHING crosses, saying so in the narrative ("Douglas,
AZ - No cattle crossed today."). Counting reports therefore understates the
collapse, and counting them without checking would have looked perfectly fine:

    year   reports   zero-crossing   CATTLE CROSSED
    2023     157           0              157
    2024     225           0              225
    2025      69           0               69
    2026      12           5                7      <- 12 reports, 7 crossings

So crossing_days() reads the narrative and excludes the no-crossing days. Same
shape of mistake as the uppercase-TOTALS bug: a rule inferred from years of
history that silently changed in the current year.

It stays a proxy, not a count -- a crossing day covers whatever crossed that
day, at whatever size -- so it belongs on the page labelled as days, beside the
Census head counts. Its virtue is currency: Census runs about six weeks behind,
this is current to yesterday.
"""
import re
import argparse
from datetime import date, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

import snowflake_db as db

MARS_BASE = "https://marsapi.ams.usda.gov/services/v1.2"

# kind is what the row is FOR, so the page can query by purpose rather than by
# slug number. Spanish editions (3674, 3630) are deliberately excluded -- same
# content, and storing both would double every count.
BORDER_SLUGS = {
    3629: ("status", "U.S. - Mexico Livestock Imports/Exports"),
    3486: ("commentary", "Mexico to United States Feeder Cattle Import Summary"),
}

COLUMNS = ["report_date", "report_begin", "report_end", "published_date",
           "slug_id", "kind", "title", "special_notes", "narrative"]


def init_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS border_reports (
            report_date TEXT NOT NULL,
            report_begin TEXT,
            report_end TEXT,
            published_date TEXT,
            slug_id INTEGER NOT NULL,
            kind TEXT,
            title TEXT,
            special_notes TEXT,
            narrative TEXT,
            PRIMARY KEY (slug_id, report_date)
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


def ingest(conn, since: date, until: date, verbose=True):
    """
    Store one row per (slug, report_date).

    A plain upsert is safe here, unlike replacement_sales: these reports carry a
    single narrative per date rather than many line items, so the natural key
    really is unique.
    """
    auth = get_auth()
    s, u = since.strftime("%m/%d/%Y"), until.strftime("%m/%d/%Y")
    n = 0
    for slug_id, (kind, title) in BORDER_SLUGS.items():
        try:
            r = requests.get(f"{MARS_BASE}/reports/{slug_id}", auth=auth,
                             params={"q": f"report_begin_date={s}:{u}"},
                             timeout=(5, 60))
            r.raise_for_status()
            rows = r.json().get("results", [])
        except Exception as e:
            if verbose:
                print(f"  [!] {slug_id}: {type(e).__name__}: {e}")
            continue

        # One report can return several identical rows; keep the last per date.
        by_date = {}
        for x in rows:
            iso = _mdy_to_iso(x.get("report_date"))
            if iso:
                by_date[iso] = x
        for iso, x in sorted(by_date.items()):
            db.merge_replace(
                conn, "border_reports", COLUMNS,
                (iso, _mdy_to_iso(x.get("report_begin_date")),
                 _mdy_to_iso(x.get("report_end_date")),
                 _mdy_to_iso(x.get("published_date")), slug_id, kind, title,
                 x.get("special_notes"), x.get("report_narrative")),
                ["slug_id", "report_date"])
            n += 1
        if verbose:
            print(f"  {title[:52]:<52} {len(by_date):>4} dates")
    conn.commit()
    return n


def current_status(conn):
    """
    Latest trade-status note, or None if the most recent report carried none.

    Absence is meaningful and is NOT backfilled from an older week: a week with
    no note is a week AMS did not flag a suspension, and quietly showing last
    month's warning as though it were current would be worse than showing
    nothing. The caller gets the date so it can say how old the note is.
    """
    row = conn.cursor().execute(
        "SELECT report_date, report_begin, report_end, special_notes, narrative "
        "FROM border_reports WHERE kind = 'status' "
        "ORDER BY report_date DESC").fetchall()
    if not row:
        return None
    rd, begin, end, notes, narr = row[0]
    return {"date": str(db.iso(rd)), "begin": str(db.iso(begin)) if begin else None,
            "end": str(db.iso(end)) if end else None,
            "notes": notes, "narrative": narr,
            "weeks_with_notes": sum(1 for r in row if r[3]),
            "weeks_total": len(row)}


# "No cattle crossed today." and its variants. Matched against the narrative to
# separate a published report from an actual crossing -- see the module note.
NO_CROSSING = re.compile(r"no cattle (crossed|were crossed|crossing)", re.I)

# Crossings appear in the narrative as "Douglas, AZ - ...", but the case is
# inconsistent (DOUGLAS, AZ), several are named in one report
# ("DOUGLAS AND NOGALES, AZ"), and a preceding sentence can run into the name
# ("... OTHERWISE NOTED. NOGALES, AZ"). Matching a state suffix and then
# normalizing against a known list is what makes the yearly counts add up.
KNOWN_PORTS = ["SANTA TERESA, NM", "ST TERESA, NM", "COLUMBUS, NM",
               "DOUGLAS, AZ", "NOGALES, AZ", "PRESIDIO, TX", "EAGLE PASS, TX",
               "LAREDO, TX", "DEL RIO, TX", "SAN LUIS, AZ", "CALEXICO, CA"]
# ST TERESA is AMS's own typo for SANTA TERESA and must fold into it, or the
# 2024 count splits 136/56 across two names for one crossing.
PORT_ALIASES = {"ST TERESA, NM": "SANTA TERESA, NM"}


def _ports_in(text: str):
    """Every known crossing named anywhere in a narrative, normalized."""
    t = (text or "").upper()
    found = set()
    for p in KNOWN_PORTS:
        city = p.split(",")[0]
        # "DOUGLAS AND NOGALES, AZ" names Douglas without its own state suffix,
        # so match the city name and require its state to appear nearby.
        for m in re.finditer(r"\b" + re.escape(city) + r"\b", t):
            tail = t[m.end():m.end() + 30]
            if p.split(", ")[1] in tail or ", " + p.split(", ")[1] in tail:
                found.add(PORT_ALIASES.get(p, p))
                break
    return found


def crossing_days(conn):
    """
    {year: {"reports", "crossings", "zero"}} -- the volume proxy, per year.

    "crossings" is the number to show. See the module docstring for why it is
    not simply the report count.
    """
    rows = conn.cursor().execute(
        "SELECT report_date, narrative FROM border_reports "
        "WHERE kind = 'commentary'").fetchall()
    out = {}
    for rd, narr in rows:
        y = str(db.iso(rd))[:4]
        d = out.setdefault(y, {"reports": 0, "crossings": 0, "zero": 0})
        d["reports"] += 1
        text = " ".join(str(narr or "").split())
        if text and NO_CROSSING.search(text):
            d["zero"] += 1
        else:
            d["crossings"] += 1
    return dict(sorted(out.items()))


def ports_by_year(conn):
    """
    {port: {year: crossing days}} -- which crossings are actually open.

    The single clearest picture of the suspension: five crossings were active
    through 2024 and only Douglas, AZ has reported cattle in 2026. Zero-crossing
    days are excluded, so a port that publishes "no cattle crossed" does not
    count as open.
    """
    rows = conn.cursor().execute(
        "SELECT report_date, narrative FROM border_reports "
        "WHERE kind = 'commentary'").fetchall()
    out = {}
    for rd, narr in rows:
        text = " ".join(str(narr or "").split())
        if not text or NO_CROSSING.search(text):
            continue
        y = str(db.iso(rd))[:4]
        for p in _ports_in(text):
            out.setdefault(p, {})
            out[p][y] = out[p].get(y, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -sum(kv[1].values())))


def reporting_days(conn):
    """Back-compat shim: {year: crossing days}. Prefer crossing_days()."""
    return {y: d["crossings"] for y, d in crossing_days(conn).items()}


def recent_commentary(conn, limit=12):
    """Latest port-level market notes, newest first."""
    rows = conn.cursor().execute(
        "SELECT report_date, narrative FROM border_reports "
        "WHERE kind = 'commentary' AND narrative IS NOT NULL "
        "ORDER BY report_date DESC").fetchall()
    out = []
    for rd, narr in rows[:limit]:
        text = " ".join(str(narr).split())
        # The narrative leads with the crossing, e.g. "Douglas, AZ - Compared
        # to Tuesday...". Split it out so the page can group by port.
        port, _, rest = text.partition(" - ")
        out.append({"date": str(db.iso(rd)),
                    "port": port.strip() if rest else None,
                    "text": rest.strip() if rest else text})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None, help="ISO date (default: 2023-01-01)")
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()

    conn = db.get_conn()
    init_tables(conn)
    if not args.show:
        since = date.fromisoformat(args.since) if args.since else date(2023, 1, 1)
        print(f"Fetching border reports, {since} .. today\n")
        n = ingest(conn, since, date.today())
        print(f"\nstored {n:,} rows")

    st = current_status(conn)
    if st:
        print(f"\n=== trade status, week of {st['begin']} .. {st['end']} ===")
        print("   " + (" ".join(str(st["notes"]).split()) if st["notes"]
                       else "(no status note on the latest report)"))
        print(f"   {st['weeks_with_notes']} of {st['weeks_total']} stored weeks "
              f"carry a note")
    print("\n=== crossing days per year (volume proxy) ===")
    print(f"   {'yr':<6}{'reports':>8}{'crossed':>9}{'zero':>6}")
    for y, d in crossing_days(conn).items():
        print(f"   {y:<6}{d['reports']:>8}{d['crossings']:>9}{d['zero']:>6}  "
              f"{'#' * max(1, d['crossings'] // 5)}")

    pby = ports_by_year(conn)
    years = sorted({y for d in pby.values() for y in d})
    print("\n=== which crossings are open (crossing days) ===")
    print(f"   {'crossing':<20}" + "".join(f"{y:>7}" for y in years))
    for port, d in pby.items():
        print(f"   {port:<20}" + "".join(f"{d.get(y, 0):>7}" for y in years))

    print("\n=== latest commentary ===")
    for c in recent_commentary(conn, 5):
        print(f"   {c['date']}  {str(c['port'] or '-'):<16} {c['text'][:74]}")
    conn.close()


if __name__ == "__main__":
    main()
