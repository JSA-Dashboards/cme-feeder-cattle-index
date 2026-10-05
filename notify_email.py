"""
Build the daily FCI estimate email. Does NOT send -- writes the subject and an
HTML body to .tmp/ for scripts/send_email.ps1 to deliver.

Split that way so the numbers and the delivery can fail independently: this file
is tested like any other query, and a broken mail path cannot take the figures
with it.

The delivery itself has moved twice and neither reason was Python's. It used
Outlook COM, for no stored credentials at all, until 2026-09-11, when the NEW
Outlook (olk.exe) turned out to have no COM interface. SMTP cannot replace it
either -- the tenant enforces security defaults, so basic auth is permanently
off. What actually delivers is a OneDrive drop that a Power Automate flow picks
up. See scripts/send_email.ps1, which carries the full account.

    python notify_email.py                 # today's run, morning slot inferred
    python notify_email.py --slot pm
    python notify_email.py --failed "update_exit=4 push_exit=0"
    python notify_email.py --stdout        # print the text body, send nothing

INTERNAL DISTRIBUTION ONLY. The body carries CME's published index values,
which are licensed to JSA for internal display and internal non-display use.
Forwarding this to clients or into a newsletter needs a separate agreement with
CME. The footer says so, because the person forwarding it will not remember.
"""
import argparse
import os
from datetime import date, datetime, timedelta

from dotenv import load_dotenv

load_dotenv()

import snowflake_db as db
from index_dates import headline_index_date

TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tmp")
DASHBOARD = "https://jsa-livestock.streamlit.app/cme-feeder-cattle-index"

BLUE, MUTED, GREEN, RED = "#1f6feb", "#6b7280", "#15803d", "#b91c1c"


def _money(v):
    return f"${v:,.2f}" if v is not None else "—"


def _signed(v):
    if v is None:
        return "—"
    return f"{'+' if v >= 0 else '−'}${abs(v):,.2f}"


def pending_print_date(cur):
    """
    The index date the email leads with -- see index_dates.py for the rule and
    why it is not MAX(report_date). Tested there; this only supplies the dates.
    """
    row = cur.execute("SELECT MAX(report_date) FROM cme_ftp_daily").fetchone()
    last_pub = date.fromisoformat(str(db.iso(row[0]))) if row and row[0] else None
    avail = {date.fromisoformat(str(db.iso(r[0])))
             for r in cur.execute("SELECT report_date FROM fci_daily")}
    pick = headline_index_date(last_pub, avail)
    return pick.isoformat() if pick else None


def gather(index_date=None):
    """Everything the email needs, from the backend the dashboard reads."""
    conn = db.get_conn()
    cur = conn.cursor()
    ph = db.placeholders(1)

    if index_date is None:
        index_date = pending_print_date(cur)

    d = {"index_date": index_date}
    r = cur.execute(
        f"SELECT fci_value, total_head, n_locations, same_day_price, same_day_head, "
        f"same_day_avg_weight FROM fci_daily WHERE report_date = {ph}", (index_date,)
    ).fetchone()
    d["value"], d["head"], d["locs"] = (r[0], r[1], r[2]) if r else (None, None, None)
    d["sd_price"], d["sd_head"], d["sd_wt"] = (r[3], r[4], r[5]) if r else (None, None, None)

    prev = cur.execute(
        f"SELECT fci_value FROM fci_daily WHERE report_date < {ph} "
        f"ORDER BY report_date DESC LIMIT 1", (index_date,)).fetchone()
    d["dod"] = (d["value"] - prev[0]) if (r and prev) else None

    # The 7-day window, as the dashboard shows it: each day's OWN head, weight
    # and price. Not total_head -- that column is the rolling 7-day total for
    # the index date on that row, so using it here would print the window total
    # seven times over and make the days look enormous and identical.
    start = (date.fromisoformat(index_date) - timedelta(days=6)).isoformat()
    d["window"] = [
        (str(db.iso(a)), b, c, e) for a, b, c, e in cur.execute(
            f"SELECT report_date, same_day_head, same_day_avg_weight, same_day_price "
            f"FROM fci_daily WHERE report_date BETWEEN {ph} AND {ph} "
            f"ORDER BY report_date", (start, index_date)).fetchall()
    ]

    # CME's latest print, and how our frozen call for THAT date did.
    c = cur.execute("SELECT report_date, fci_value, total_head FROM cme_ftp_daily "
                    "ORDER BY report_date DESC LIMIT 1").fetchone()
    if c:
        d["cme_date"], d["cme_value"], d["cme_head"] = str(db.iso(c[0])), c[1], c[2]
        f = cur.execute(
            f"SELECT fci_value, total_head FROM fci_snapshots WHERE index_date = {ph} "
            f"AND run_slot = 'am' ORDER BY captured_at LIMIT 1", (d["cme_date"],)).fetchone()
        d["scored_call"], d["scored_head"] = (f[0], f[1]) if f else (None, None)
        d["peers"] = cur.execute(
            f"SELECT source, fci_value FROM peer_estimates WHERE index_date = {ph} "
            f"ORDER BY source", (d["cme_date"],)).fetchall()
    else:
        d["cme_date"] = d["cme_value"] = d["cme_head"] = None
        d["scored_call"] = d["scored_head"] = None
        d["peers"] = []

    # PEERS FOR THE DATE WE ARE PUBLISHING, not just the one CME already
    # printed. The block above scores a settled call after the fact; this is the
    # live check, and it is the one that mattered on 2026-10-05 -- CIH and
    # Compass both had 337.76 while we published 339.55, and the only thing that
    # caught the $1.79 was Ross reading CIH's sheet on X by eye.
    d["live_peers"] = cur.execute(
        f"SELECT source, fci_value FROM peer_estimates WHERE index_date = {ph} "
        f"ORDER BY source", (index_date,)).fetchall()

    # HAS THIS NUMBER MOVED SINCE THE LAST TIME WE SENT IT?
    #
    # The gap that cost 2026-10-05. The 07:45 call went to clients at 339.55 and
    # the 13:00 run settled it at 337.74, and nothing anywhere said it had moved
    # -- the afternoon email simply carried a different number as if it had
    # always been that. Same shape on 2026-10-01: an 08:00 call of 337.1512, a
    # 13:07 settle of 336.8375, 217 head of late Superior Video in between.
    #
    # EXACT AND THRESHOLD-FREE: the previous snapshot of THIS index date is a
    # number we stored, so the move is arithmetic, not an estimate. Measured
    # over 44 index dates the median move is 0.0000 and six moved a dime or
    # more, the worst 2.0199 -- so it is quiet on an ordinary day, which is the
    # only reason it is worth printing on the days it is not.
    prior = cur.execute(
        f"SELECT fci_value, total_head, run_slot, run_date, captured_at "
        f"FROM fci_snapshots WHERE index_date = {ph} "
        f"ORDER BY captured_at DESC", (index_date,)).fetchall()
    # Skip snapshots that ARE this run's own -- update_index freezes one before
    # the email is built -- and take the last call that said something
    # different. The run DATE goes in the label: this date's snapshots span
    # several days once CME is behind, so "the 08:12 am call" alone would be
    # ambiguous about which morning.
    d["prior_call"] = None
    for v, h, slot_, rd, cap in prior:
        if d["value"] is not None and abs(v - d["value"]) <= 1e-9 and h == d["head"]:
            continue
        d["prior_call"] = {"value": v, "head": h, "slot": slot_,
                           "at": str(cap)[11:16], "date": str(db.iso(rd))}
        break

    # Is today's sample whole? barn_report owns the roster logic and returns
    # strings; nothing here re-derives it, so the email and the run log cannot
    # disagree about which barns are out.
    try:
        import barn_report
        d["barn_lines"] = list(barn_report.report_lines(conn))
    except Exception as e:                     # noqa: BLE001 -- diagnostics only
        d["barn_lines"] = ["Barn report unavailable: %s" % e]

    conn.close()
    d["ingest_warnings"] = _ingest_warnings()
    return d


def _ingest_warnings():
    """
    Rows the parsers could not classify on the last run, written by
    update_index._record_ingest_warnings().

    Absence is NOT an error -- a fresh checkout has no sidecar and that reads
    as "nothing to report", which is also what an ordinary day looks like.
    """
    import json
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "data", "ingest_warnings.json")
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:                          # noqa: BLE001
        return []


def _mdy(iso):
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%-m/%-d/%y") \
        if os.name != "nt" else datetime.strptime(iso, "%Y-%m-%d").strftime("%m/%d/%y").lstrip("0").replace("/0", "/")


def build(d, slot="am", failed=None):
    if failed:
        subject = f"JSA FCI pipeline FAILED — {date.today().strftime('%m/%d/%y')}"
        body = (
            f'<div style="font:14px system-ui,Segoe UI,Arial">'
            f'<p style="color:{RED};font-weight:700;font-size:16px">'
            f'The FCI pipeline did not complete.</p>'
            f'<p>Exit codes: <code>{failed}</code></p>'
            f'<p>No new estimate was published, so the dashboard is showing the '
            f'previous run\'s numbers. Check '
            f'<code>logs\\update_{date.today().isoformat()}.log</code>, and '
            f'<code>scripts\\check_run.ps1</code> for a summary.</p>'
            f'<p style="color:{MUTED};font-size:12px">Exit 5 means the refresh '
            f'worked and only the Snowflake publish failed — local data is '
            f'current but the dashboard is stale.</p></div>')
        return subject, body

    label = "morning call" if slot == "am" else "settled pass"
    dod = _signed(d["dod"])
    subject = (f"JSA FCI {_mdy(d['index_date'])}: {_money(d['value'])} "
               f"({dod} DoD)" if d["value"] is not None else
               f"JSA FCI {_mdy(d['index_date'])}: no value")

    colour = GREEN if (d["dod"] or 0) >= 0 else RED
    rows = ""
    for iso, head, wt, price in d["window"]:
        dt = datetime.strptime(iso, "%Y-%m-%d")
        rows += (
            f'<tr><td style="padding:4px 10px 4px 0">{dt.strftime("%a %m/%d")}</td>'
            f'<td align="right" style="padding:4px 10px">{int(head or 0):,}</td>'
            f'<td align="right" style="padding:4px 10px">'
            f'{f"{wt:,.0f} lb" if wt else "—"}</td>'
            f'<td align="right" style="padding:4px 0">'
            f'{_money(price) if price else "—"}</td></tr>')

    # --- Did this number move since we last sent it? -------------------------
    moved = ""
    p = d.get("prior_call")
    if p and d["value"] is not None:
        delta = d["value"] - p["value"]
        dh = (d["head"] or 0) - (p["head"] or 0)
        loud = abs(delta) >= 0.05
        moved = (
            f'<p style="margin:18px 0 6px;font-weight:600'
            f'{";color:" + RED if loud else ""}">'
            f'{"Moved" if loud else "Changed"} since the '
            f'{p["slot"]} call of {_mdy(p["date"])} {p["at"]}</p>'
            f'<p style="margin:0;font:13px system-ui,Segoe UI,Arial">'
            f'{_money(p["value"])} &rarr; <b>{_money(d["value"])}</b> '
            f'(<b>{delta:+.4f}</b>){f" on {dh:+,} head" if dh else ""}</p>'
            + (f'<p style="margin:4px 0 0;color:{RED};font:13px system-ui,'
               f'Segoe UI,Arial">If the {p["at"]} figure went to anyone, it is '
               f'now {abs(delta):,.2f} out of date.</p>' if loud else ""))

    # --- The live peer check -------------------------------------------------
    # Ahead of everything else in the body, because it is the one line that
    # would have stopped 2026-10-05 leaving the building. Both desks had 337.76
    # and we published 339.55; nothing in this email mentioned them, so the
    # $1.79 was caught by a human opening CIH's feed by eye.
    peercheck = ""
    if d["value"] is not None:
        if d.get("live_peers"):
            prow = ""
            worst = 0.0
            for src, val in d["live_peers"]:
                gap = d["value"] - val
                worst = max(worst, abs(gap))
                prow += (f'<tr><td style="padding:3px 12px 3px 0">'
                         f'{src if src == "CIH" else src.title()}</td>'
                         f'<td align="right">{_money(val)}</td>'
                         f'<td align="right" style="padding-left:12px;color:'
                         f'{GREEN if abs(gap) < 0.05 else RED}">'
                         f'{gap:+.4f}</td></tr>')
            # 5 cents: the project's own bar is "matches CME to the cent", and
            # measured agreement with CME since 2026-08-28 is inside half a
            # cent. Anything past a nickel against BOTH desks has meant a real
            # defect every time it has happened.
            flag = ("" if worst < 0.05 else
                    f'<p style="margin:4px 0 0;color:{RED};font-weight:600">'
                    f'We are {worst:,.2f} from a published peer. Every time that '
                    f'has exceeded a nickel it has been our bug, not theirs — '
                    f'check the ingest before sending this out.</p>')
            peercheck = (
                f'<p style="margin:18px 0 6px;font-weight:600">Against the desks, '
                f'same index date</p><table style="font:13px system-ui,Segoe UI,'
                f'Arial"><tr><td style="padding:3px 12px 3px 0">JSA</td>'
                f'<td align="right"><b>{_money(d["value"])}</b></td>'
                f'<td align="right"></td></tr>{prow}</table>{flag}')
        else:
            peercheck = (
                f'<p style="margin:18px 0 6px;color:{MUTED};font:13px system-ui,'
                f'Segoe UI,Arial">No CIH or Compass estimate recorded for '
                f'{_mdy(d["index_date"])} yet — nothing is checking this number '
                f'against an outside source. '
                f'<code>python add_peer_estimate.py --date {d["index_date"]} '
                f'--source CIH --value &lt;x&gt;</code></p>')

    # --- Is the sample whole? ------------------------------------------------
    barn = ""
    if d.get("barn_lines"):
        head_line = d["barn_lines"][0]
        rest = [l.strip() for l in d["barn_lines"][1:]]
        colour = MUTED if not rest else RED
        barn = (f'<p style="margin:18px 0 6px;font-weight:600">Sample</p>'
                f'<p style="margin:0;font:13px system-ui,Segoe UI,Arial;color:'
                f'{colour}">{head_line}</p>')
        if rest:
            barn += ('<ul style="margin:4px 0 0 18px;padding:0;font:13px '
                     'system-ui,Segoe UI,Arial">'
                     + "".join(f"<li>{l}</li>" for l in rest) + "</ul>")

    # --- Rows the parsers could not classify ---------------------------------
    # The 2026-10-05 warning, delivered. It was always produced; it went into a
    # log file on a droplet that nothing reads and that deletes itself after 30
    # days, which is why a $1.82 error reached clients with the job exiting 0.
    warn = ""
    if d.get("ingest_warnings"):
        li = "".join(
            f'<li>{w["source"]} {w["report_date"]}: '
            f'{int(w["head"] or 0):,} head at {float(w["avg_weight"] or 0):,.0f} lb, '
            f'{_money(w["avg_price"])}, grade {w["muscle_grade"]}</li>'
            for w in d["ingest_warnings"])
        warn = (f'<p style="margin:18px 0 6px;color:{RED};font-weight:600">'
                f'{len(d["ingest_warnings"])} row(s) NOT ingested — the parser '
                f'could not read their delivery label</p>'
                f'<p style="margin:0 0 4px;font:13px system-ui,Segoe UI,Arial;'
                f'color:{MUTED}">These are not exclusions. They are rows in the '
                f'index weight band that we failed to classify, so this estimate '
                f'is short by them.</p>'
                f'<ul style="margin:0 0 0 18px;padding:0;font:13px system-ui,'
                f'Segoe UI,Arial">{li}</ul>')

    scored = ""
    if d["cme_value"] is not None:
        bits = [f'<tr><td style="padding:3px 12px 3px 0">CME published '
                f'{_mdy(d["cme_date"])}</td><td align="right"><b>'
                f'{_money(d["cme_value"])}</b></td><td align="right" '
                f'style="padding-left:12px;color:{MUTED}">'
                f'{int(d["cme_head"] or 0):,} head</td></tr>']
        if d["scored_call"] is not None:
            miss = d["scored_call"] - d["cme_value"]
            same = int(d["scored_head"] or 0) == int(d["cme_head"] or 0)
            bits.append(
                f'<tr><td style="padding:3px 12px 3px 0">JSA frozen morning call</td>'
                f'<td align="right">{_money(d["scored_call"])}</td>'
                f'<td align="right" style="padding-left:12px;color:'
                f'{GREEN if abs(miss) < 0.05 else MUTED}">{miss:+.4f}'
                f'{" · identical window" if same else ""}</td></tr>')
        for src, val in d["peers"]:
            bits.append(
                f'<tr><td style="padding:3px 12px 3px 0">{src.title() if src != "CIH" else "CIH"}</td>'
                f'<td align="right">{_money(val)}</td>'
                f'<td align="right" style="padding-left:12px;color:{MUTED}">'
                f'{val - d["cme_value"]:+.4f}</td></tr>')
        scored = (f'<p style="margin:18px 0 6px;font-weight:600">Last CME print, '
                  f'and how we scored</p><table style="font:13px system-ui,Segoe UI,Arial'
                  f'">{"".join(bits)}</table>')

    daily = ""
    if d["sd_price"]:
        daily = (f'<p style="color:{MUTED};margin:4px 0 0">Same-day: '
                 f'{_money(d["sd_price"])} on {int(d["sd_head"] or 0):,} head, '
                 f'{d["sd_wt"]:,.0f} lb average</p>')

    body = f"""<div style="font:14px system-ui,Segoe UI,Arial;color:#111;max-width:620px">
<p style="margin:0;color:{MUTED};font-size:12px;letter-spacing:.05em;
text-transform:uppercase">JSA FCI Estimate · {label}</p>
<p style="margin:2px 0 0;font-size:30px;font-weight:700;color:{BLUE}">
{_money(d['value'])}</p>
<p style="margin:2px 0 0;font-size:15px;color:{colour};font-weight:600">
{dod} day over day</p>
<p style="margin:6px 0 0">Index date <b>{_mdy(d['index_date'])}</b> ·
{int(d['head'] or 0):,} head across {d['locs'] or 0} locations</p>
{daily}
{moved}
{warn}
{peercheck}
{barn}
<p style="margin:18px 0 6px;font-weight:600">7-day window</p>
<table style="font:13px system-ui,Segoe UI,Arial;border-collapse:collapse">
<tr style="color:{MUTED};border-bottom:1px solid #e5e7eb">
<td style="padding:0 10px 4px 0">Day</td><td align="right" style="padding:0 10px 4px">Head</td>
<td align="right" style="padding:0 10px 4px">Weight</td>
<td align="right" style="padding:0 0 4px">Price</td></tr>
{rows}</table>
{scored}
<p style="margin:20px 0 0"><a href="{DASHBOARD}" style="color:{BLUE}">
Open the dashboard</a></p>
<p style="margin:16px 0 0;color:{MUTED};font-size:11px;border-top:1px solid #e5e7eb;
padding-top:8px">Generated automatically by the JSA FCI pipeline.
<b>Internal use only.</b> This message contains CME Feeder Cattle Index values,
licensed to John Stewart &amp; Associates for internal display and internal
non-display use. Do not forward to clients or reproduce in client
communications without a separate agreement with CME.</p></div>"""
    return subject, body


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", choices=("am", "pm"), default=None)
    ap.add_argument("--date", default=None, help="index date (default: latest)")
    ap.add_argument("--failed", default=None, help="exit-code summary; sends a failure notice")
    ap.add_argument("--stdout", action="store_true", help="print and write nothing")
    args = ap.parse_args()

    slot = args.slot
    if slot is None:
        from snapshots import run_slot
        slot = run_slot()

    d = {} if args.failed else gather(args.date)
    subject, body = build(d, slot=slot, failed=args.failed)

    if args.stdout:
        print(subject)
        print()
        import re
        print(re.sub(r"<[^>]+>", "", body).replace("&amp;", "&").strip())
        return

    os.makedirs(TMP, exist_ok=True)
    with open(os.path.join(TMP, "email_subject.txt"), "w", encoding="utf-8") as f:
        f.write(subject)
    with open(os.path.join(TMP, "email_body.html"), "w", encoding="utf-8") as f:
        f.write(body)
    print(f"email content written to .tmp/ ({subject})")


if __name__ == "__main__":
    main()
