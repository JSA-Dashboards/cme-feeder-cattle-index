"""
Build the daily FCI estimate email. Does NOT send -- writes the subject and an
HTML body to .tmp/ for scripts/send_email.ps1 to hand to Outlook.

Split that way on purpose. Sending through Outlook COM needs no stored
credentials at all: it uses the profile already authenticated on this machine,
so there is no app password in .env, no SMTP AUTH exemption to request from IT,
and no third-party mail service holding a key. PowerShell speaks COM natively;
Python would need pywin32. So Python does the numbers and PowerShell does the
sending.

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

TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tmp")
DASHBOARD = "https://jsa-livestock.streamlit.app/cme-feeder-cattle-index"

BLUE, MUTED, GREEN, RED = "#1f6feb", "#6b7280", "#15803d", "#b91c1c"


def _money(v):
    return f"${v:,.2f}" if v is not None else "—"


def _signed(v):
    if v is None:
        return "—"
    return f"{'+' if v >= 0 else '−'}${abs(v):,.2f}"


def gather(index_date=None):
    """Everything the email needs, from the backend the dashboard reads."""
    conn = db.get_conn()
    cur = conn.cursor()
    ph = db.placeholders(1)

    if index_date is None:
        row = cur.execute("SELECT MAX(report_date) FROM fci_daily").fetchone()
        index_date = str(db.iso(row[0]))

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

    conn.close()
    return d


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
                f'<tr><td style="padding:3px 12px 3px 0">JSA frozen 07:30 call</td>'
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
