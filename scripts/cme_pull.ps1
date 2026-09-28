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
# The main pipeline runs at 07:30 and 13:00. 07:30 is ALWAYS before CME
# publishes -- the earliest print ever observed is an hour later -- so the
# morning run can never carry yesterday's official number. The 13:00 run is the
# first poll that sees it. That left the dashboard's "Last CME Print" tile and
# the whole forecast scorecard running about four hours behind CME every
# morning: a 09:05 print did not reach the page until 13:05.
#
# 10:15 clears the 10:05 worst case with ten minutes to spare.
#
# This job deliberately does NOT recompute anything. CME's published value does
# not feed our estimate -- it is what the estimate is SCORED AGAINST -- so the
# work is only: fetch the file, store it, push those tables. About 15 seconds
# against the main pipeline's 12 minutes.
#
# NO HEALTHCHECK PING, on purpose. This run is an accelerator, not a guarantee:
# if it fails, the 13:00 run pulls the same file with a 10-day lookback and the
# only cost is that the print appears at 13:05 as it always used to. Monitoring
# it would add an alert for a condition that self-heals within three hours.

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
    Log ("CME pull failed (exit {0}). The 13:00 run will retry with a wider " +
         "lookback; nothing else is affected." -f $code)
    exit $code
}

# Push only the three tables this job can have changed. Re-uploading all
# thirteen would take a minute to land two tables' worth of new rows, and would
# also republish the border and replacement data mid-morning for no reason.
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
} else {
    Log ("ERROR: push failed (exit {0}). SQLite has the print; the dashboard " +
         "will pick it up at 13:00." -f $pushCode)
}

Log ("CME print pull finished  pull_exit={0}  push_exit={1}  {2}" -f `
     $code, $pushCode, (Get-Date -Format 'HH:mm:ss'))

if ($pushCode -ne 0) { exit 5 }
exit 0
