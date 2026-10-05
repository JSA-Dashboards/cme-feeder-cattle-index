"""
Record a competitor's published FCI estimate, so ours can be scored against
theirs on the same index date once CME prints.

    python add_peer_estimate.py --date 2026-09-08 --source CIH --value 327.43
    python add_peer_estimate.py --list

--date is CME's INDEX date -- the date the estimate is FOR -- not the date the
sheet was issued. That distinction matters because the two sources head their
sheets differently:

  CIH      heads the sheet with the index date it is estimating
           ("Daily Feeder Cattle Index Estimate 09/08/2026" -> --date 2026-09-08)
  Compass  heads the sheet with the ISSUE date and names the index date in the
           body ("9/9 7:30AM MST ... Tuesday, September 8, 2026 $326.05"
           -> --date 2026-09-08, NOT 09-09)

Only record a figure the source presents as its own ESTIMATE. Both sheets also
carry the last CME-published value for reference -- CIH as "Previous FCI",
Compass as the "Monday" figure beside its headline -- and logging those as a
competitor's estimate would credit them with a number they simply copied. On
2026-09-08 both showed 326.98 there, which was CME's actual 09-07 print.

Upserts, so a mistyped figure is fixed by re-running with the right one. Writes
to SQLite; the daily job's Snowflake push carries it to the dashboard.
"""
import argparse
import re
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

import snowflake_db as db

KNOWN_SOURCES = ("CIH", "COMPASS")

# CIH posts the estimate to x.com/CIHCattleTeam as TEXT, not only as the image
# on their sheet, and the format is stable:
#
#   Feeder Cattle Index +$0.92
#   CIH Est: $337.76; Previous: $336.84
#   8,703 head dropping off (40% of index); 7,082 head traded (35%)
#
# --paste takes that verbatim so the figure is copied rather than retyped.
# Typing it is where the transcription errors live, and this is the check that
# would have caught five of the six worst mornings on record.
#
# NOT FETCHED AUTOMATICALLY, and not for want of trying: x.com renders through
# JavaScript, so a plain GET returns 200 with none of the post text in it;
# nitter.net is dead and r.jina.ai answers 403. Automating it needs a headless
# browser on the droplet or the paid API, neither of which is worth it for one
# number a day. One paste is.
_CIH_EST = re.compile(r"CIH\s+Est:?\s*\$?\s*([0-9]+(?:\.[0-9]+)?)", re.I)
_CIH_PREV = re.compile(r"Previous:?\s*\$?\s*([0-9]+(?:\.[0-9]+)?)", re.I)
_CIH_TRADED = re.compile(r"([0-9,]+)\s+head\s+traded", re.I)


def parse_cih_post(text):
    """
    {value, previous, head_traded} from a CIH post, or None if it is not one.

    Returns None rather than guessing: a post that is not the daily index
    tweet -- and most of that account is not -- must not be recorded as an
    estimate. The seminar advert pinned to the top of the feed is the obvious
    way that goes wrong.
    """
    m = _CIH_EST.search(text or "")
    if not m:
        return None
    out = {"value": float(m.group(1)), "previous": None, "head_traded": None}
    p = _CIH_PREV.search(text)
    if p:
        out["previous"] = float(p.group(1))
    t = _CIH_TRADED.search(text)
    if t:
        out["head_traded"] = int(t.group(1).replace(",", ""))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="CME index date the estimate is FOR (YYYY-MM-DD)")
    ap.add_argument("--source", help="e.g. CIH, COMPASS")
    ap.add_argument("--value", type=float, help="their estimate, $/cwt")
    ap.add_argument("--note", default=None, help="optional free text")
    ap.add_argument("--list", action="store_true", help="show what is recorded and exit")
    ap.add_argument("--paste", help="a CIH post pasted verbatim; fills --source "
                                    "and --value from it")
    args = ap.parse_args()

    if args.paste:
        got = parse_cih_post(args.paste)
        if not got:
            raise SystemExit(
                "that does not look like a CIH index post -- expected a line "
                "like 'CIH Est: $337.76; Previous: $336.84'")
        args.source = args.source or "CIH"
        args.value = args.value if args.value is not None else got["value"]
        bits = []
        if got["previous"] is not None:
            bits.append("their previous %.2f" % got["previous"])
        if got["head_traded"] is not None:
            bits.append("%s head traded" % format(got["head_traded"], ","))
        if bits and not args.note:
            args.note = "from x.com/CIHCattleTeam; " + ", ".join(bits)
        print("parsed CIH post: $%.2f%s" % (
            got["value"], " (%s)" % ", ".join(bits) if bits else ""))

    conn = db.get_conn()

    if args.list:
        rows = conn.cursor().execute(
            "SELECT index_date, source, fci_value, note FROM peer_estimates "
            "ORDER BY index_date DESC, source"
        ).fetchall()
        if not rows:
            print("no peer estimates recorded yet")
        else:
            print(f"{'index date':<12} {'source':<9} {'value':>9}  note")
            for r in rows:
                r = db.iso_row(r)
                print(f"{r[0]:<12} {r[1]:<9} {r[2]:>9.2f}  {r[3] or ''}")
        conn.close()
        return

    missing = [f"--{n}" for n, v in
               (("date", args.date), ("source", args.source), ("value", args.value))
               if v is None]
    if missing:
        raise SystemExit(f"need {', '.join(missing)} (or --list)")

    try:
        index_date = datetime.strptime(args.date, "%Y-%m-%d").date()
    except ValueError:
        raise SystemExit(f"--date must be YYYY-MM-DD, got {args.date!r}")

    source = args.source.strip().upper()
    if source not in KNOWN_SOURCES:
        # Not fatal: a new competitor is a legitimate reason to add a source.
        # But a typo silently creating "CHI" alongside "CIH" would split the
        # scorecard in two without ever erroring, so say something.
        print(f"note: {source!r} is not one of {KNOWN_SOURCES} -- adding it as a "
              f"new source. Check for a typo if that was not intended.")

    # Sanity band. The index has traded roughly $130-$400 over the archive's
    # span; anything outside that is a decimal slip or a head count pasted into
    # the wrong argument, and it would quietly wreck the scorecard's averages.
    if not 100.0 <= args.value <= 500.0:
        raise SystemExit(
            f"--value {args.value} is outside $100-$500/cwt; refusing it as a "
            f"likely typo. Override by editing the table directly if it is real."
        )

    db.merge_replace(
        conn, "peer_estimates",
        ["index_date", "source", "fci_value", "note"],
        (index_date.isoformat(), source, args.value, args.note),
        ["index_date", "source"],
    )
    conn.commit()

    cme = conn.cursor().execute(
        f"SELECT fci_value FROM cme_ftp_daily WHERE report_date = {db.placeholders(1)}",
        (index_date.isoformat(),),
    ).fetchone()
    ours = conn.cursor().execute(
        f"SELECT fci_value FROM fci_daily WHERE report_date = {db.placeholders(1)}",
        (index_date.isoformat(),),
    ).fetchone()
    conn.close()

    print(f"recorded {source} {index_date} = ${args.value:.2f}")
    if ours:
        print(f"  ours     ${ours[0]:.2f}  ({args.value - ours[0]:+.2f} vs them)")
    if cme:
        print(f"  CME      ${cme[0]:.2f}  -> their miss {args.value - cme[0]:+.2f}")
        if ours:
            print(f"                        our miss   {ours[0] - cme[0]:+.2f}")
    else:
        print("  CME has not published this index date yet -- pending")


if __name__ == "__main__":
    main()
