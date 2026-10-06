"""
Our head against CME's own published head, which is the slug-level check
tests/test_roster_covers_cme.py says it cannot do.

THE GAP THIS FILLS. That file answers yes or no about a NAME, never about a
slug, and says so itself: when two AMS reports print one city name -- Billings
1774/1777, Torrington 2101/2103, La Junta 1901/1903, Brush 1906/3090 -- losing
one of the pair is silent, because the survivor still makes the name reachable.
It closes with "catching it needs a slug-level comparison against mars_sales,
which is a different check."

This is that check, written because the blind spot fired. CME published 32 head
of Billings on 2026-10-02 at $350.10 and we held none: AMS runs FOUR cattle
auctions in that city and the roster carried two.

    1774  Public Auction Yards - Billings, MT (Wed)   on the roster
    1777  Billings Livestock Commission (Thu)          on the roster
    1776  Public Auction Yards - Billings, MT (Fri)    MISSING -- the 32 head
    1775  Billings Livestock Commission (Mon)          MISSING

WHY CME AND NOT AMS. mars_census.py already compares mars_sales against what AMS
serves, but it can only ask about slugs ON THE ROSTER -- a slug nobody fetches
is a question nobody asks, which is exactly how a missing one hides. CME's file
is built from the whole sample, so it sees barns our roster has never heard of.

THE HEADLINE CHECK COMPARES DAILY TOTALS AND MATCHES NO NAMES AT ALL. That is
deliberate. The first version of this module compared per-location head by name
and reported 712 head missing across five locations; all but 32 of it was
wrong, because CME truncates its location column -- at 20 characters in one file
era and 30 in another -- and cases names its own way. "Mcalester" missed our
"McAlester" on nine dates, "Mid Missouri Stockya" missed our "Mid Missouri
Stockyards Cattle Auction - Phillipsburg, MO" on eight, and "Wyoming-Nebraska D"
is truncated before the word Direct so it missed "WY DIRECT" entirely. A check
that cries wolf nine times in ten gets switched off, and then the one real
finding is invisible.

Totals need none of that. Measured over the 25 dates since 2026-08-28 where both
sides exist: short on three, over on none, and two of the three are a single
head. It is quiet on an ordinary day, which is the only thing that makes the
loud one worth reading.

SCOPED TO 2026-08-28 BY DEFAULT, when the direct-trade component was added.
Before that we genuinely held no direct or video rows and CME did, so an
unscoped scan returns 28,889 rows and 13 million head of history that is
explained and unfixable.

ONLY A SHORTFALL COUNTS. Holding MORE than CME is normal: CME's file is a
snapshot of its own print time and we keep ingesting, so a late or preliminary
report legitimately puts us ahead. Flagging that would fire every afternoon.
"""
import re

# The date the direct-trade component was added; before it, CME's file contains
# a whole class of sale we had no ingest for. See CLAUDE.md.
DIRECT_TRADE_FROM = "2026-08-28"

_STATE_WORD = {
    "colorado": "CO", "iowa": "IA", "kansas": "KS", "missouri": "MO",
    "montana": "MT", "nebraska": "NE", "new mexico": "NM", "oklahoma": "OK",
    "south dakota": "SD", "texas": "TX", "wyoming": "WY",
    "wyoming-nebraska": "WY",
}
_REGION = {"(nc)": "(north central)", "(sc)": "(south central)"}


def _norm(name):
    s = (name or "").strip().lower()
    for short, long in _REGION.items():
        s = s.replace(short, long)
    s = s.replace("_video", " video")
    return re.sub(r"\s+", " ", s)


def _direct_key(name):
    """
    ('direct', 'WY') for 'WY DIRECT', 'Wyoming Direct' and 'Wyoming-Nebraska D'
    alike. The last of those is why this tolerates a TRUNCATED "direct": CME's
    column cuts it to a single D, and the first version of this returned None
    there and then refused the match.
    """
    s = _norm(name)
    m = re.search(r"\b(d|di|dir|dire|direc|direct)\b\s*$", s)
    head = s[:m.start()].strip(" -") if m else (
        s.split("direct")[0].strip(" -") if "direct" in s else None)
    if head is None:
        return None
    if len(head) == 2 and head.upper() in _STATE_WORD.values():
        return ("direct", head.upper())
    for word, code in _STATE_WORD.items():
        if head.startswith(word) or word.startswith(head):
            return ("direct", code)
    return ("direct", head)


def matches(cme_name, our_name):
    """
    Do these two name the same market? Best effort, and used ONLY to attribute
    a shortfall the totals have already proved -- never to decide whether one
    exists. A miss here costs a name in a message, not a wrong answer.
    """
    a, b = _norm(cme_name), _norm(our_name)
    if a == b:
        return True
    dk_a, dk_b = _direct_key(cme_name), _direct_key(our_name)
    if dk_a and dk_b:
        return dk_a == dk_b
    if dk_a or dk_b:
        return False
    return len(a) >= 6 and len(b) >= 6 and (a.startswith(b) or b.startswith(a))


def daily_shortfalls(conn, since=DIRECT_TRADE_FROM):
    """
    [{date, our_head, cme_head, short}] for every date where CME's published
    same-day head exceeds ours. EMPTY is the expected result.

    No names are compared. Both sides are a single number per date that each
    party derived independently, which is the whole point.
    """
    import snowflake_db as db
    ph = db.placeholders(1).strip()
    rows = conn.cursor().execute(
        "SELECT f.report_date, f.same_day_head, t.same_day_head "
        "FROM fci_daily f JOIN cme_ftp_daily t ON t.report_date = f.report_date "
        "WHERE f.same_day_head IS NOT NULL AND t.same_day_head IS NOT NULL "
        "AND f.report_date >= {} ORDER BY f.report_date".format(ph),
        (since,)).fetchall()
    out = []
    for d, ours, cme in rows:
        ours, cme = int(ours or 0), int(cme or 0)
        if cme > ours:
            out.append({"date": str(db.iso(d))[:10], "our_head": ours,
                        "cme_head": cme, "short": cme - ours})
    return out


def locate(conn, index_date):
    """
    Best-effort attribution for one date: which CME location holds head we do
    not. Call it only on a date daily_shortfalls() has already flagged.

    Returns [{cme_location, cme_head, our_head, short}]. May be empty even on a
    real shortfall -- if the name matching above fails, the head is still
    missing and the totals still said so.
    """
    import snowflake_db as db
    from bucketing import shifted_bucket_date

    ph = db.placeholders(1).strip()
    ours = {}
    for rd, loc, hd in conn.cursor().execute(
            "SELECT report_date, location, head_count FROM mars_sales "
            "WHERE report_date BETWEEN {} AND {}".format(ph, ph),
            (index_date, index_date)).fetchall():
        if hd:
            ours[loc] = ours.get(loc, 0) + int(hd)
    # rows bucketed ONTO this date from a neighbouring one
    for rd, loc, hd in conn.cursor().execute(
            "SELECT report_date, location, head_count FROM mars_sales "
            "WHERE report_date BETWEEN {} AND {}".format(ph, ph),
            ((index_date[:8] + "01"), index_date)).fetchall():
        if hd and str(shifted_bucket_date(loc, rd))[:10] == index_date:
            ours.setdefault(loc, 0)
            if str(rd)[:10] != index_date:
                ours[loc] += int(hd)

    out = []
    for loc, hd in conn.cursor().execute(
            "SELECT location, head_count FROM cme_ftp_locations "
            "WHERE report_date = {}".format(ph), (index_date,)).fetchall():
        cme_head = int(hd or 0)
        held = sum(v for name, v in ours.items() if matches(loc, name))
        if cme_head > held:
            out.append({"cme_location": str(loc).strip(), "cme_head": cme_head,
                        "our_head": held, "short": cme_head - held})
    out.sort(key=lambda r: -r["short"])
    return out
