"""
WHERE each block of scripts/daily_update.ps1 sits, not whether it exists.

The 2026-10-01 change this guards moved ONE thing -- the estimate email --
from the end of the daily job to immediately after the critical Snowflake
push, so the morning mail stops waiting out the border, census, calf, corn
and herd ingests and the dashboard push, about twelve minutes it does not
read a single row from.

A test that greps for "send_email.ps1" would have passed before the move and
after it, and would pass again if someone put the email back. ORDER is the
whole content of the change, so every assertion here is an inequality between
two positions in the file.

THE PART THAT MUST NOT MOVE. The healthcheck success ping stays at the END.
The pipeline's guarantee against a silent failure is the ABSENCE of that ping
-- the 13:00 run that hung on 2026-09-10 was killed at its time limit, and a
killed process never reaches its own /fail line. Send the success ping before
the optional ingests and a hang in them arrives at the monitor as a green
check. So the email moving up and the ping NOT moving up are two halves of
one requirement, and both are asserted here.

PROOF THAT THESE CHECKS CAN FAIL. CLAUDE.md records three guards written for
this repo that could not fail on bad input -- one printed the same variable
under both labels, one asserted a tautology, one banned a string in comments
rather than in queries. So the ordering rules live in check_order(), and the
tests at the bottom feed it two known violations built by actually reordering
the shipped script's own text: the email put back at the end, and the ping
hoisted to before the ingests. If either mutation stops being rejected, this
file is decoration.
"""
import re
from datetime import datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "daily_update.ps1"


def _pos(text, pattern, what):
    """
    Index of the ONE match of `pattern`, or an assertion failure.

    Uniqueness is deliberate. If a marker is renamed, duplicated or deleted,
    an ordering test that quietly matched the first hit, or no hit at all,
    would report success about a file it is no longer reading. Better to
    break loudly and be re-pointed at the new wording.
    """
    hits = [m.start() for m in re.finditer(pattern, text, re.M)]
    assert len(hits) == 1, (
        f"expected exactly one {what} marker (/{pattern}/), found "
        f"{len(hits)}. Re-point this test at the current wording rather than "
        f"loosening it.")
    return hits[0]


def marks(text):
    """The positions every rule below is written against."""
    return {
        "critical_push": _pos(
            text, r"^\s*Log '--- pushing the index to Snowflake",
            "critical push"),
        "email_header": _pos(
            text, r"^# -- Daily estimate email", "email block header"),
        "email_done": _pos(
            text, r'^Log \("estimate email step done', "email dispatch stamp"),
        "imports": _pos(
            text, r"^\s*Log '--- refreshing the supply-side sources",
            "optional import ingest"),
        "herd": _pos(
            text, r"^\s*Log '--- refreshing the herd sources", "herd ingest"),
        "optional_push": _pos(
            text, r"^\s*Log '--- pushing the dashboard tables to Snowflake",
            "optional push"),
        "run_finished": _pos(
            text, r'^Log \("run finished', "run finished line"),
        "summary": _pos(
            text, r'^\$summary = \(', "healthcheck summary"),
        "success_ping": _pos(
            text, r"^\s*Ping-Health '' \$summary", "success ping"),
        "fail_ping": _pos(
            text, r"^\s*Ping-Health '/fail' \$summary", "failure ping"),
        "exit_block": _pos(
            text, r"^if \(\$code -ne 0\)\s+\{ exit \$code \}",
            "exit-code block"),
        "mail_summary": _pos(
            text, r"^\$mailSummary = \(", "mail summary declaration"),
    }


def _send_calls(text):
    """Positions of real send_email.ps1 INVOCATIONS, not comment mentions.

    The first draft of the rules below matched the bare string, and the script
    names send_email.ps1 in a comment above the block. CLAUDE.md lists exactly
    this mistake among the three guards that could not fail: "one banned a
    string in comments rather than in queries."
    """
    pos, seen = [], 0
    for line in text.splitlines(keepends=True):
        if not line.lstrip().startswith("#") and "send_email.ps1" in line:
            pos.append(seen)
        seen += len(line)
    return pos


def check_order(text):
    """Every ordering rule, as a list of violations. Empty list = correct."""
    m = marks(text)
    bad = []

    def before(a, b, why):
        if not m[a] < m[b]:
            bad.append(f"{a} must come before {b}: {why}")

    # The move itself: mail goes out between the publish and the dashboards.
    before("critical_push", "email_header",
           "the mail reads fci_daily, cme_ftp_daily, fci_snapshots and "
           "peer_estimates, which only exist in Snowflake once the "
           "--critical-only push has committed them")
    before("email_done", "imports",
           "the whole point of the 2026-10-01 move is that the mail no longer "
           "waits for the optional ingests")

    # Not one send_email.ps1 call may sit below the ingests -- there are two
    # (the success path and the failure path) and checking only the block
    # header would miss a stray one.
    for pos in _send_calls(text):
        if pos > m["imports"]:
            bad.append("a send_email.ps1 call appears after the optional "
                       "ingests begin; the mail must leave before them")

    # The ingests keep their established order (CLAUDE.md: "Step order in the
    # daily job is load-bearing ... Do not reorder this").
    before("imports", "herd", "the ingest order is load-bearing")
    before("herd", "optional_push", "push what the ingests produced, after")

    # The dead-man's switch. Absence of the success ping is the ONLY signal a
    # hang produces, so it may not be sent until the hang-prone work is done.
    before("optional_push", "run_finished", "the log line reports every exit "
           "code, including the optional push's")
    before("run_finished", "summary", "the summary is stamped at the finish")
    before("optional_push", "summary",
           "a summary built before the ingests would carry the wrong clock")
    for name in ("success_ping", "fail_ping"):
        before("optional_push", name,
               "a ping sent before the optional ingests would satisfy the "
               "monitor in advance, so a hang in them -- the 2026-09-10 "
               "failure -- would never alert")
        before("summary", name, "the ping must send the summary, not an "
                                "empty string")
    before("success_ping", "exit_block", "the exit-code block stays last")

    # $mailSummary must be built before anything sends it. PowerShell does not
    # error on an undefined variable -- it interpolates empty -- so a
    # declaration that drifted below its use would mail a failure summary
    # reading "update_exit= cme_exit= push_exit=" and nothing would complain.
    for pos in _send_calls(text):
        if pos < m["mail_summary"]:
            bad.append("a send_email.ps1 call appears before $mailSummary is "
                       "declared; PowerShell would interpolate it empty rather "
                       "than fail")

    # The stamp exists to MEASURE the dispatch, so it has to follow it. Hoisted
    # above, it still satisfies "before imports" and still prints a plausible
    # time -- of the wrong instant.
    last_send = max(_send_calls(text))
    if m["email_done"] < last_send:
        bad.append("the 'estimate email step done' stamp is above the dispatch "
                   "it stamps, so the time it records is not the time the mail "
                   "left")

    # THE SWITCH CANNOT BE DEFEATED BY ADDITION EITHER. Every rule above checks
    # where the known success ping SITS; none would notice a second one added
    # earlier, which satisfies the monitor in advance just as completely. A
    # /fail ping anywhere is fine -- it only ever reports trouble -- and the
    # /start ping and the venv-missing /fail are both deliberate and early.
    sends_success = [h.start() for h in re.finditer(r"Ping-Health\s+''", text)]
    if len(sends_success) != 1:
        bad.append(f"there are {len(sends_success)} bare success pings; there "
                   f"must be exactly one, after the optional push")
    for pos in sends_success:
        if pos < m["optional_push"]:
            bad.append("a success ping is sent before the optional push; the "
                       "monitor would be satisfied in advance and a hang in "
                       "the ingests would never alert")

    # The two summaries must stay two. The ping's is stamped when the run
    # FINISHED; the mail's when the index was PUBLISHED, now about twelve
    # minutes earlier. One shared variable would put a plausible, wrong time
    # on whichever call site inherited it.
    if "$mailSummary" not in text:
        bad.append("the email no longer builds its own $mailSummary; it is "
                   "sharing the healthcheck's $summary and therefore its "
                   "finish-time stamp")
    else:
        mail_decl = re.search(r"^\$mailSummary = \(.*", text, re.M)
        if not mail_decl or "publish_step=" not in mail_decl.group(0):
            bad.append("$mailSummary is not stamped publish_step=")
        if mail_decl and "finished=" in mail_decl.group(0):
            bad.append("$mailSummary claims a finish time the run has not "
                       "reached when the mail goes out")
        if not re.search(r"^\$summary = \(.*finished=", text, re.M):
            bad.append("$summary is not stamped finished=")
        if re.search(r"send_email\.ps1'\) -Failed \$summary", text):
            bad.append("the failure email sends the healthcheck's $summary, "
                       "which is stamped at a time that has not happened yet "
                       "when the mail goes out")
    return bad


@pytest.fixture(scope="module")
def script_text():
    return SCRIPT.read_text(encoding="utf-8")


def test_the_shipped_script_is_in_the_right_order(script_text):
    assert check_order(script_text) == []


def test_the_email_leaves_before_the_ten_minute_tail(script_text):
    """The saving, stated as the inequality that produces it."""
    m = marks(script_text)
    assert m["critical_push"] < m["email_header"] < m["email_done"] < m["imports"]


def test_the_success_ping_still_certifies_the_whole_run(script_text):
    m = marks(script_text)
    assert m["email_done"] < m["imports"] < m["optional_push"] < m["success_ping"]


# --- the guards, proved able to fail ----------------------------------------
#
# Each mutation is cut from the shipped script's own text and re-inserted
# somewhere wrong, so it is the real block in a real file, not a toy string
# that might miss the pattern for an unrelated reason.

def _cut(text, start_pat, end_pat):
    """Remove the block from start_pat through the end of end_pat's line."""
    s = re.search(start_pat, text, re.M).start()
    e = text.index("\n", re.search(end_pat, text, re.M).start()) + 1
    return text[:s] + text[e:], text[s:e]


def test_the_check_rejects_the_email_being_put_back_at_the_end(script_text):
    """The pre-2026-10-01 layout: the mail waits for the dashboards."""
    rest, block = _cut(script_text, r"^# -- Daily estimate email",
                       r'^Log \("estimate email step done')
    moved = rest.rstrip("\n") + "\n\n" + block
    bad = check_order(moved)
    assert any("no longer waits" in b for b in bad), bad
    assert any("must leave before them" in b for b in bad), bad


def test_the_check_rejects_the_success_ping_being_hoisted(script_text):
    """
    The regression that would matter most: a well-meaning follow-up decides
    the ping may as well move up with the email. It may not -- that is the
    2026-09-10 hang going unreported.
    """
    rest, block = _cut(script_text, r"^\$summary = \(",
                       r"^\}\s*$\n\n# Distinct exit codes")
    anchor = re.search(r"^# Everything past this point is dashboards", rest, re.M)
    moved = rest[:anchor.start()] + block + "\n" + rest[anchor.start():]
    bad = check_order(moved)
    assert any("would never alert" in b for b in bad), bad


def test_the_check_rejects_one_shared_summary(script_text):
    """
    Giving the mail the healthcheck's finish-stamped string.

    Rejection can arrive two ways here and both count: as a violation from
    check_order, or as the duplicate-marker assertion in _pos, because
    collapsing the two variables leaves two `$summary = (` declarations. What
    must not happen is the file being accepted.
    """
    broken = script_text.replace("$mailSummary", "$summary")
    try:
        bad = check_order(broken)
    except AssertionError as e:
        assert "found 2" in str(e), e
        return
    assert bad, "a shared summary variable was accepted"


# --- the slot does not change -----------------------------------------------

def test_the_check_rejects_the_mail_summary_drifting_below_its_use(script_text):
    """PowerShell interpolates an undefined variable empty rather than failing."""
    decl = re.search(r"^\$mailSummary = \(.*?\n(?:.*?\n)*?.*?\)\s*$",
                     script_text, re.M)
    assert decl, "could not find the $mailSummary declaration to move"
    block = decl.group(0)
    moved = script_text.replace(block + "\n", "", 1)
    anchor = 'Log ("estimate email step done'
    moved = moved.replace(anchor, block + "\n" + anchor, 1)
    bad = check_order(moved)
    assert any("before $mailSummary is declared" in b for b in bad), bad


def test_the_check_rejects_the_stamp_being_hoisted_above_the_dispatch(script_text):
    """A stamp above the send still reads plausibly, and times the wrong thing."""
    stamp = re.search(r'^Log \("estimate email step done.*\n', script_text, re.M)
    assert stamp, "could not find the dispatch stamp"
    moved = script_text.replace(stamp.group(0), "", 1)
    moved = moved.replace("if ($code -eq 0 -and $pushCode -eq 0) {",
                          stamp.group(0) + "if ($code -eq 0 -and $pushCode -eq 0) {", 1)
    bad = check_order(moved)
    assert any("above the dispatch it stamps" in b for b in bad), bad


def test_the_check_rejects_a_second_success_ping_added_early(script_text):
    """Moving the ping is caught; ADDING one earlier defeats the switch equally."""
    extra = "Ping-Health '' $summary\n"
    sabotaged = script_text.replace(
        "Log '--- refreshing the supply-side sources",
        extra + "Log '--- refreshing the supply-side sources", 1)
    # Either route is a rejection: marks() refuses a duplicated marker outright,
    # and check_order() catches a differently-worded second ping. Both must not
    # pass silently.
    try:
        bad = check_order(sabotaged)
    except AssertionError as e:
        assert "success ping" in str(e), e
        return
    assert any("bare success pings" in b or "before the optional push" in b
               for b in bad), bad


def test_both_the_old_and_new_send_times_are_still_the_morning_slot():
    """
    Item the move hangs on: run_slot() decides whether any mail is sent at
    all. Moving the dispatch from ~08:25 to ~08:13 must not cross the
    boundary, so check the real function rather than assuming it.
    """
    from snapshots import AM_PM_BOUNDARY_HOUR, run_slot

    assert AM_PM_BOUNDARY_HOUR == 11
    for hh, mm in ((8, 13), (8, 25), (7, 45), (10, 59)):
        assert run_slot(datetime(2026, 10, 2, hh, mm)) == "am", (hh, mm)
    assert run_slot(datetime(2026, 10, 2, 11, 0)) == "pm"
