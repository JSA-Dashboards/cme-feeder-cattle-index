# CME print pull. Registered in Task Scheduler as "JSA FCI CME print pull",
# 10:15 local, daily. Appends to the same logs/update_<date>.log as the main job.
#
# WHY 10:15, AND WHY A THIRD RUN AT ALL.
#
# CME publishes each index date's file on the NEXT business day. Measured from
# their own FTP MDTM timestamps over the last 14 files:
#
#     earliest 08:35    median 09:05    latest 10:05    (all Central)
#
# The main pipeline runs at 07:45 and 13:00. 07:45 is well ahead of the median
# print (09:05) and of the latest ever observed (10:05), so the morning run
# cannot be relied on to carry yesterday's official number. The 13:00 run is
# the first poll that reliably sees it. That left the dashboard's "Last CME
# Print" tile and the whole forecast scorecard running about four hours behind
# CME every morning: a 09:05 print did not reach the page until 13:05.
#
# NOTE, since the morning run moved 07:30 -> 08:00 on 2026-09-29 and then
# 08:00 -> 07:45 on 2026-10-01: this used to read "07:30 is ALWAYS before CME
# publishes", and that absolute stopped being safe to assert at 08:00. The
# wider n=17 sample in scripts/README-schedule.md puts the EARLIEST print at
# 08:05 Central, and the morning run does not reach its own CME step until
# several minutes in, so on a rare early day it may catch the file. How many
# minutes in is NOT measured -- the log stamps only the run's start and finish,
# not the CME step -- so this stays "may", not a number. What is certain is the
# direction: starting 15 minutes earlier moves that step 15 minutes earlier
# too, so an early catch is rarer at 07:45 than it was at 08:00, never more
# likely. Either way it is a bonus, not a guarantee, and not a reason to drop
# this job -- on a median day the file still does not exist for another hour.
#
# 10:15 clears the 10:05 worst case with ten minutes to spare.
#
# This job deliberately does NOT recompute anything. CME's published value does
# not feed our estimate -- it is what the estimate is SCORED AGAINST -- so the
# work is only: fetch the file, store it, push those tables. About 15 seconds
# against the main pipeline's 12 minutes.
#
# NO HEALTHCHECK PING, on purpose. This run is an accelerator, not a guarantee:
# if it FAILS TO LOAD, the 13:00 run pulls the same file with a 10-day lookback
# and the only cost is that the print appears at 13:05 as it always used to.
# Monitoring that would add an alert for a condition that self-heals within
# three hours.
#
# THAT REASONING DOES NOT COVER PUSH EXIT 3, and saying it did was the same
# untruth the log line below used to carry. Exit 3 is "the push COMMITTED and
# the contents disagree with local SQLite": Snowflake is serving new wrong
# values, not old right ones, and nothing about three hours passing repairs
# that. It self-heals only if the 13:00 push happens to write correctly what
# this one wrote wrongly -- which is what happened on 2026-09-29, but the root
# cause is still unknown, so it is a hope and not a mechanism. What exit 3 gets
# today is the ERROR line below; check_run.ps1's digest greps the log for
# 'ERROR', so it does reach the next morning's summary. It does NOT page
# anybody, and that gap is deliberate-for-now rather than argued: adding a
# healthcheck slot here is the open item.

$repo = 'C:\Users\RossBaldwin\projects\cme-feeder-cattle-index'
$py   = Join-Path $repo '.venv\Scripts\python.exe'

$logDir = Join-Path $repo 'logs'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log = Join-Path $logDir ('update_{0}.log' -f (Get-Date -Format 'yyyy-MM-dd'))

function Log([string]$m) { Add-Content -Path $log -Value $m -Encoding utf8 }

Log ''
Log ('-' * 70)
Log ("CME print pull started  {0}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss K'))

if (-not (Test-Path $py)) { Log 'FATAL: venv python missing'; exit 2 }

# A 4-day lookback, not 10: this runs daily and only needs to catch the print
# that just landed plus anything from a long weekend. The main job keeps its
# wider net for genuine gaps.
$start = (Get-Date).AddDays(-4).ToString('yyyy-MM-dd')

$o = Join-Path $env:TEMP ('cme_o_{0}.txt' -f $PID)
$e = Join-Path $env:TEMP ('cme_e_{0}.txt' -f $PID)
try {
    $p = Start-Process -FilePath $py -ArgumentList '-u', 'backfill_ftp.py', '--start', $start `
            -WorkingDirectory $repo -NoNewWindow -Wait -PassThru `
            -RedirectStandardOutput $o -RedirectStandardError $e
    $code = $p.ExitCode
} catch {
    Log ("FATAL: could not start the CME pull - {0}" -f $_.Exception.Message)
    exit 3
}
foreach ($f in @($o, $e)) {
    if (Test-Path $f) {
        $c = Get-Content $f | Where-Object { $_ -ne '' }
        if ($c) { $c | ForEach-Object { Log $_ } }
        Remove-Item $f -Force
    }
}

if ($code -ne 0) {
    # NOTE THE OUTER PARENTHESES around the concatenation. PowerShell's -f
    # binds TIGHTER than +, so ("a {0} " + "b." -f $x) formats only the SECOND
    # string and logs a literal "{0}" for the first -- which is exactly what
    # this line and the push line below did until 2026-09-30, printing
    # "CME pull failed (exit {0})" on every failure and hiding the one number
    # a reader needs. daily_update.ps1 always wrapped; this file did not.
    Log (("CME pull failed (exit {0}). The 13:00 run will retry with a wider " +
          "lookback; nothing else is affected.") -f $code)
    exit $code
}

# Push only the three tables this job can have changed. A bare run pushes all
# TWENTY-ONE (8 CRITICAL + 13 OPTIONAL in 02_migrate_data.py -- this comment
# said "thirteen", which is the optional count alone, and then "twenty" before
# pipeline_stamp joined CRITICAL), taking a minute to land two tables' worth of
# new rows, and would also republish the border and replacement data
# mid-morning for no reason.
#
# NOTE pipeline_stamp is deliberately NOT in the --tables list below. It is the
# dashboard's "last refreshed" marker, and this job changes no index numbers --
# stamping it here would advance the freshness clock without the reconstruction
# having run, which is the one lie that check exists to prevent.
$pushCode = 0
$po = Join-Path $env:TEMP ('cme_po_{0}.txt' -f $PID)
$pe = Join-Path $env:TEMP ('cme_pe_{0}.txt' -f $PID)
try {
    $pp = Start-Process -FilePath $py -ArgumentList '-u', 'snowflake/02_migrate_data.py', `
            '--tables', 'cme_ftp_daily,cme_ftp_locations,cme_ftp_brackets' `
            -WorkingDirectory $repo -NoNewWindow -Wait -PassThru `
            -RedirectStandardOutput $po -RedirectStandardError $pe
    $pushCode = $pp.ExitCode
} catch {
    Log ("WARN: could not start the Snowflake push - {0}" -f $_.Exception.Message)
    $pushCode = 4
}
foreach ($f in @($po, $pe)) {
    if (Test-Path $f) {
        $c = Get-Content $f | Where-Object { $_ -ne '' }
        if ($c) { $c | ForEach-Object { Log $_ } }
        Remove-Item $f -Force
    }
}

if ($pushCode -eq 0) {
    Log 'CME print pull OK - the published print and scorecard are current'
} elseif ($pushCode -eq 3) {
    # Exit 3 is "every table LOADED, but its contents do not match local
    # SQLite", and the generic message below is FALSE for it in the way that
    # matters most. That message says the dashboard is merely behind and the
    # 13:00 run will carry the print across; here the write COMMITTED, so the
    # dashboard is not waiting on anything -- it is serving NEW WRONG VALUES
    # right now, and re-running at 13:00 pushes the same local rows again.
    #
    # This job pushes cme_ftp_daily, cme_ftp_locations and cme_ftp_brackets,
    # and all three are in 02_migrate_data.py's CRITICAL_TABLES, so exit 3 is
    # genuinely reachable from here and not a code only the daily job can see.
    # Those three back the "Last CME Print" tile and the forecast scorecard --
    # the numbers our estimate is scored against.
    #
    # Worded to match daily_update.ps1's exit-3 branch on purpose: two logs
    # describing the same condition in different words is how an incident gets
    # misread at 10:15.
    Log (("ERROR: the CME push COMMITTED but CONTENT VERIFICATION FAILED " +
          "(exit {0}). This is NOT the stale-dashboard case: Snowflake holds " +
          "NEW data that disagrees with local SQLite, so the DASHBOARD MAY BE " +
          "SERVING WRONG VALUES for the published print and the scorecard. " +
          "Waiting for the 13:00 run does NOT fix this. The differing tables " +
          "and columns are in the push output above - read them before " +
          "re-pushing. This is the shape of the 2026-09-29 incident.") -f $pushCode)
} else {
    # A load failure rolls each table back on its own, so Snowflake still holds
    # its previous contents and the 13:00 run really does catch it up.
    Log (("ERROR: push failed (exit {0}). SQLite has the print; the dashboard " +
          "will pick it up at 13:00.") -f $pushCode)
}

Log ("CME print pull finished  pull_exit={0}  push_exit={1}  {2}" -f `
     $code, $pushCode, (Get-Date -Format 'HH:mm:ss'))

if ($pushCode -ne 0) { exit 5 }
exit 0
