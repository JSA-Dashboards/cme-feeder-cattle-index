"""
What scripts/cme_pull.ps1 actually LOGS, by running it.

WHY THIS IS NOT ANOTHER SOURCE-TEXT ASSERTION. The existing check on the
daily job (test_the_daily_job_says_something_true_about_exit_3) greps the
script for "$pushCode -eq 3" and for some words. That proves the text is
present. It cannot prove the branch is REACHED, that it is reached for the
right exit code, or that the message renders -- and the bug this file guards
against is precisely a message that is present in the source and wrong on the
page.

Both failure modes have already happened in this one file:

  * The else arm claimed "SQLite has the print; the dashboard will pick it up
    at 13:00." For exit 3 that is false. Exit 3 means the push COMMITTED and
    the contents disagree with local SQLite, so Snowflake is serving NEW WRONG
    values for the published print and the scorecard; nothing about waiting
    three hours repairs it. A grep for the sentence passes either way, because
    the sentence is still there -- correctly -- for the other exit codes.

  * Every failure line in this file logged a literal "{0}" instead of the exit
    code until 2026-09-30, because PowerShell binds -f tighter than +, so
    ("a {0} " + "b." -f $x) formats only the second string. That is invisible
    to any test that looks at the source, and it hid the one number a reader
    needs. A test that RUNS the script sees "{0}" immediately.

HOW IT RUNS WITHOUT TOUCHING ANYTHING REAL. The script is copied to a temp
tree and its one hardcoded $repo line is repointed there; the test asserts
that this is the ONLY line that differs, so it cannot silently be exercising
a doctored script. Start-Process is then shadowed by a PowerShell function of
the same name (functions win over cmdlets), which returns the exit code the
case is about. So backfill_ftp.py never runs, 02_migrate_data.py never runs,
no FTP is contacted and NOTHING IS EVER PUSHED TO SNOWFLAKE -- while the real
if/elseif/else, the real -f formatting and the real exit codes all execute.
"""
import os
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "cme_pull.ps1"

POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")

pytestmark = pytest.mark.skipif(
    os.name != "nt" or POWERSHELL is None or not SCRIPT.exists(),
    reason="needs Windows PowerShell and scripts/cme_pull.ps1")

REPO_LINE = re.compile(r"^\$repo\s*=\s*'.*'\s*$", re.M)

WRAPPER = """\
$ErrorActionPreference = 'Stop'

# Shadows the cmdlet: a function of the same name wins name resolution, so the
# real script's Start-Process calls land here and nothing is ever launched.
function Start-Process {{
    param(
        [string]$FilePath,
        [string[]]$ArgumentList,
        [string]$WorkingDirectory,
        [switch]$NoNewWindow,
        [switch]$Wait,
        [switch]$PassThru,
        [string]$RedirectStandardOutput,
        [string]$RedirectStandardError
    )
    $joined = $ArgumentList -join ' '
    if ($joined -match '02_migrate_data') {{
        [pscustomobject]@{{ ExitCode = {push} }}
    }} else {{
        [pscustomobject]@{{ ExitCode = {pull} }}
    }}
}}

. '{script}'

# `exit` inside a dot-sourced script sets $LASTEXITCODE but does NOT stop the
# outer -File script, which would otherwise return 0 and make every exit-code
# assertion below vacuous. Re-raise it as this process's status.
exit $LASTEXITCODE
"""


def run_pull(tmp_path, push_code, pull_code=0):
    """(exit_code, log_text) from really executing the script."""
    root = tmp_path / "repo"
    (root / ".venv" / "Scripts").mkdir(parents=True)
    # The script refuses to start without this; contents are never read.
    (root / ".venv" / "Scripts" / "python.exe").write_text("stub")

    src = SCRIPT.read_text(encoding="utf-8")
    # A lambda, not a template: a Windows temp path contains backslashes and
    # re would read "\Users" as an escape. Same trap CLAUDE.md records for
    # TOML -- "\U" starts a unicode escape and blows up the whole thing.
    copy_src, n = REPO_LINE.subn(lambda _m: f"$repo = '{root}'", src, count=1)
    assert n == 1, "could not find the single $repo assignment to repoint"

    # The copy must differ from the original in that ONE line and nothing else,
    # or this test is not exercising the shipped script.
    a, b = src.splitlines(), copy_src.splitlines()
    assert len(a) == len(b)
    differing = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
    assert len(differing) == 1, (
        f"the temp copy differs from the shipped script on {len(differing)} "
        f"lines, not 1: {differing}")

    script = root / "cme_pull.ps1"
    script.write_text(copy_src, encoding="utf-8")
    wrapper = tmp_path / "wrapper.ps1"
    wrapper.write_text(
        WRAPPER.format(push=push_code, pull=pull_code,
                       script=str(script).replace("'", "''")),
        encoding="utf-8")

    proc = subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(wrapper)],
        capture_output=True, text=True, cwd=str(root))

    log = root / "logs" / f"update_{date.today():%Y-%m-%d}.log"
    text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    assert text, (f"the script wrote no log.\nstdout: {proc.stdout}\n"
                  f"stderr: {proc.stderr}")
    return proc.returncode, text


STALE = "will pick it up at 13:00"


def test_exit_3_is_not_logged_as_a_merely_stale_dashboard(tmp_path):
    """
    The one that matters. Exit 3 means Snowflake COMMITTED values that
    disagree with local SQLite, so the tables backing the "Last CME Print"
    tile and the forecast scorecard may be wrong right now. Telling the reader
    to wait for 13:00 sends them away from an incident.
    """
    code, log = run_pull(tmp_path, push_code=3)

    assert STALE not in log, (
        "exit 3 was logged as a stale dashboard that the 13:00 run will fix. "
        "The write COMMITTED; waiting does not repair it.\n" + log)

    lower = log.lower()
    assert "committed" in lower, "the log does not say the write committed"
    assert "wrong values" in lower, (
        "the log does not warn that the dashboard may be serving wrong values")
    assert "error" in lower, (
        "exit 3 must log at ERROR -- check_run.ps1's digest greps for it, and "
        "it is the only way this reaches a human")
    assert code == 5, f"expected the script to exit 5, got {code}"


def test_the_exit_code_reaches_the_log_instead_of_a_literal_placeholder(tmp_path):
    """
    PowerShell binds -f tighter than +, so a concatenation that is not
    parenthesised formats only its last fragment. Every failure line in this
    script logged "{0}" for months because of it.
    """
    for push_code in (1, 3, 4):
        _, log = run_pull(tmp_path / f"c{push_code}", push_code=push_code)
        assert "{0}" not in log, (
            f"push exit {push_code} logged a literal '{{0}}' instead of the "
            f"number -- the -f/+ precedence bug is back:\n{log}")
        assert f"{push_code}" in log, (
            f"the actual exit code {push_code} never reaches the log:\n{log}")


def test_an_ordinary_load_failure_still_gets_the_reassuring_message(tmp_path):
    """
    The control, and the reason the exit-3 branch had to be added rather than
    the old sentence simply deleted. A load failure DOES roll back, so
    Snowflake really does still hold its previous contents and the 13:00 run
    really does catch it up. If this test ever fails, the fix for exit 3 has
    been applied too broadly.
    """
    code, log = run_pull(tmp_path, push_code=1)
    assert STALE in log, (
        "a rolled-back load no longer gets the accurate stale-dashboard "
        "message:\n" + log)
    assert "wrong values" not in log.lower(), (
        "a rolled-back load is being reported as possible corruption, which "
        "would train the reader to ignore the real thing")
    assert code == 5


def test_a_clean_run_says_so_and_exits_0(tmp_path):
    """The other control: no false alarm on the happy path."""
    code, log = run_pull(tmp_path, push_code=0)
    assert "CME print pull OK" in log
    assert "ERROR" not in log
    assert code == 0


def test_the_comment_counts_the_tables_a_bare_push_would_send():
    """
    The comment justifying --tables says how many a bare run would upload.
    It said "thirteen", which is the OPTIONAL count alone; a bare run sends
    all twenty. Small, but it is the reason the flag is there, and the repo's
    standard is that a stated number matches an independently derived one.
    """
    migrate = (REPO / "snowflake" / "02_migrate_data.py").read_text(encoding="utf-8")
    total = 0
    for listname in ("CRITICAL_TABLES", "OPTIONAL_TABLES"):
        m = re.search(rf"^{listname}\s*=\s*\[(.*?)\]", migrate, re.S | re.M)
        assert m, f"cannot find {listname}"
        total += len(re.findall(r'"([a-z_]+)"', m.group(1)))

    words = {13: "THIRTEEN", 20: "TWENTY", 21: "TWENTY-ONE", 19: "NINETEEN"}
    src = SCRIPT.read_text(encoding="utf-8").upper()
    assert words.get(total, str(total)) in src, (
        f"a bare push now sends {total} tables; cme_pull.ps1's comment does "
        f"not say so. Update it or the next reader will size the saving wrong.")


def test_the_wording_matches_the_daily_jobs_exit_3_branch(tmp_path):
    """
    Two logs describing the same condition in different words is how an
    incident gets misread at 10:15. Both jobs can reach exit 3 and both push
    CRITICAL tables, so the phrases a reader greps for must be the same.
    """
    daily = REPO / "scripts" / "daily_update.ps1"
    if not daily.exists():
        pytest.skip("daily_update.ps1 not present")
    _, log = run_pull(tmp_path, push_code=3)
    branch = daily.read_text(encoding="utf-8")
    if "$pushCode -eq 3" not in branch:
        pytest.skip("daily_update.ps1 has no exit-3 branch to match")
    branch = branch.split("$pushCode -eq 3", 1)[1].split("} else {", 1)[0].lower()
    for phrase in ("content verification failed", "wrong values", "committed"):
        assert phrase in log.lower(), f"cme_pull.ps1 never says {phrase!r}"
        assert phrase in branch, (
            f"daily_update.ps1 no longer says {phrase!r}; the two exit-3 "
            f"messages have drifted apart")
