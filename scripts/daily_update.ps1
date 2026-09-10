# Daily FCI pipeline refresh. Registered in Task Scheduler as "JSA FCI daily update"
# (see scripts/README-schedule.md). Runs update_index.py against the repo's venv and
# appends stdout/stderr to logs/update_<date>.log.
#
# TWO TRIGGERS, one script. 07:30 local is the morning call -- the number that goes
# out and the one comparable to CIH's and Compass's morning sheets. 13:00 is the
# settled pass, and it exists because USDA publication is not finished by 07:30:
# measured 2026-09-09 over 80 auctions and 246 reports, 85.2% of a sale day's
# qualifying head is fetchable by 07:30 the next morning, but 95.8% by noon. Nearly
# all of that gap is OKC West (El Reno), which publishes its previous-day sale at a
# median of +1 day 11:13 and had missed the morning run 7 times out of 7; folding its
# 09/08 sale in moved that date's estimate +0.33, about 80x the scorecard's MAE.
#
# The script needs no argument to tell the runs apart: snapshots.run_slot() reads the
# clock, files anything before 11:00 as 'am' and the rest as 'pm', and freezes each
# slot's estimate INSERT-OR-IGNORE so the afternoon pass cannot overwrite the morning
# call it is meant to be compared against.
#
# Working directory MUST be the repo root: update_index.py calls load_dotenv(), which
# resolves .env relative to the current directory, and that's where MARS_API_KEY lives.

$repo = 'C:\Users\RossBaldwin\projects\cme-feeder-cattle-index'
$py   = Join-Path $repo '.venv\Scripts\python.exe'

$logDir = Join-Path $repo 'logs'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log = Join-Path $logDir ('update_{0}.log' -f (Get-Date -Format 'yyyy-MM-dd'))

function Log([string]$m) { Add-Content -Path $log -Value $m -Encoding utf8 }

Log ''
Log ('=' * 70)
Log ("run started  {0}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss K'))

if (-not (Test-Path $py))                    { Log 'FATAL: venv python missing'; exit 2 }
if (-not (Test-Path (Join-Path $repo '.env'))) { Log 'FATAL: .env missing - MARS_API_KEY unavailable'; exit 3 }

# Separate temp files, then fold into the log. Redirecting a native exe's stderr
# inside PowerShell 5.1 wraps each line in an ErrorRecord and corrupts $? -- this
# avoids that entirely.
$outFile = Join-Path $env:TEMP ('fci_out_{0}.txt' -f $PID)
$errFile = Join-Path $env:TEMP ('fci_err_{0}.txt' -f $PID)

try {
    # -u forces unbuffered stdout. Without it Python buffers everything until
    # exit, so a redirected log stays EMPTY for the whole run and there is no
    # way to tell slow progress (pdfplumber parsing ~20 PDFs is CPU-heavy and
    # can take 15+ minutes) from an actual hang.
    $p = Start-Process -FilePath $py -ArgumentList '-u', 'update_index.py' `
            -WorkingDirectory $repo -NoNewWindow -Wait -PassThru `
            -RedirectStandardOutput $outFile -RedirectStandardError $errFile
    $code = $p.ExitCode
} catch {
    Log ("FATAL: could not start python - {0}" -f $_.Exception.Message)
    exit 4
}

if (Test-Path $outFile) { Get-Content $outFile | Where-Object { $_ -ne '' } | ForEach-Object { Log $_ }; Remove-Item $outFile -Force }
if (Test-Path $errFile) {
    $e = Get-Content $errFile
    if ($e) { Log '--- stderr ---'; $e | ForEach-Object { Log $_ } }
    Remove-Item $errFile -Force
}

# Pull CME's OWN published index values (backfill_ftp.py -> cme_ftp_daily /
# cme_ftp_locations, over anonymous FTP at ftp.cmegroup.com). This was missing
# from the first version of this script, which meant the reconstruction was
# refreshed daily while CME's published series stayed frozen at whatever date
# it was last backfilled -- so the "Last CME Print" tile could only ever go
# stale, and there was nothing to score the forecast against.
#
# Idempotent (INSERT OR REPLACE per date), so a 10-day lookback is free and
# catches files that land late. Deliberately NON-fatal: a stale CME series is
# a much smaller problem than skipping the publish entirely, and CME's FTP is
# a third party we do not control.
$cmeCode = 0
if ($code -eq 0) {
    $cmeStart = (Get-Date).AddDays(-10).ToString('yyyy-MM-dd')
    Log ("--- pulling CME published prints since {0} ---" -f $cmeStart)
    $cOut = Join-Path $env:TEMP ('fci_cme_out_{0}.txt' -f $PID)
    $cErr = Join-Path $env:TEMP ('fci_cme_err_{0}.txt' -f $PID)
    try {
        $cp = Start-Process -FilePath $py -ArgumentList '-u', 'backfill_ftp.py', '--start', $cmeStart `
                -WorkingDirectory $repo -NoNewWindow -Wait -PassThru `
                -RedirectStandardOutput $cOut -RedirectStandardError $cErr
        $cmeCode = $cp.ExitCode
    } catch {
        Log ("WARN: could not start the CME pull - {0}" -f $_.Exception.Message)
        $cmeCode = 7
    }
    foreach ($f in @($cOut, $cErr)) {
        if (Test-Path $f) {
            $c = Get-Content $f | Where-Object { $_ -ne '' }
            if ($c) { $c | ForEach-Object { Log $_ } }
            Remove-Item $f -Force
        }
    }
    if ($cmeCode -ne 0) {
        Log ("WARN: CME pull failed (exit {0}). Continuing - the reconstruction and " +
             "publish are unaffected, but 'Last CME Print' will stay stale." -f $cmeCode)
    }
}

# Fold any WAL contents back into the .db before the push reads it.
# update_index.py normally checkpoints on close, but an interrupted run can
# leave rows stranded in data/mars_history.db-wal, and the push would then
# upload a dataset missing that day's rows.
if ($code -eq 0) {
    try {
        & $py -c "import sqlite3,os; d=os.path.join(r'$repo','data','mars_history.db'); c=sqlite3.connect(d); c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); print('wal checkpointed; fci rows =', c.execute('select count(*) from fci_daily').fetchone()[0]); c.close()" 2>&1 |
            ForEach-Object { Log $_ }
    } catch {
        Log ("WARN: wal checkpoint failed - {0}" -f $_.Exception.Message)
    }
}

# Push SQLite -> Snowflake. THIS is the step that refreshes the dashboard:
# production reads JSA.CME_FEEDER_CATTLE, not the local file. Skipped when the
# USDA refresh failed -- publishing a partial dataset is worse than publishing
# yesterday's complete one. Connection settings come from .env; the key
# passphrase from SNOWFLAKE_PRIVATE_KEY_PASSPHRASE in the environment.
$pushCode = 0
if ($code -eq 0) {
    Log '--- pushing to Snowflake (JSA.CME_FEEDER_CATTLE) ---'
    $pOut = Join-Path $env:TEMP ('fci_push_out_{0}.txt' -f $PID)
    $pErr = Join-Path $env:TEMP ('fci_push_err_{0}.txt' -f $PID)
    try {
        $pp = Start-Process -FilePath $py -ArgumentList '-u', 'snowflake/02_migrate_data.py' `
                -WorkingDirectory $repo -NoNewWindow -Wait -PassThru `
                -RedirectStandardOutput $pOut -RedirectStandardError $pErr
        $pushCode = $pp.ExitCode
    } catch {
        Log ("FATAL: could not start the Snowflake push - {0}" -f $_.Exception.Message)
        $pushCode = 6
    }
    foreach ($f in @($pOut, $pErr)) {
        if (Test-Path $f) {
            $c = Get-Content $f | Where-Object { $_ -ne '' }
            if ($c) { $c | ForEach-Object { Log $_ } }
            Remove-Item $f -Force
        }
    }
    if ($pushCode -eq 0) {
        Log 'Snowflake push OK - dashboard is serving current data'
    } else {
        Log ("ERROR: Snowflake push failed (exit {0}). Local SQLite is current but " +
             "the DASHBOARD IS STALE - each table rolls back individually, so " +
             "Snowflake still holds its previous contents." -f $pushCode)
    }
} else {
    Log 'skipping Snowflake push: the USDA refresh failed, nothing good to publish'
}

Log ("run finished  update_exit={0}  cme_exit={1}  push_exit={2}  {3}" -f $code, $cmeCode, $pushCode, (Get-Date -Format 'HH:mm:ss'))

# Prune logs older than 30 days so this doesn't grow without bound.
Get-ChildItem $logDir -Filter 'update_*.log' -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } |
    Remove-Item -Force -ErrorAction SilentlyContinue

# Distinct exit codes so Task Scheduler's LastTaskResult says WHICH half failed:
# the update itself, or only the publish step.
if ($code -ne 0)      { exit $code }
elseif ($pushCode -ne 0) { exit 5 }
else                  { exit 0 }
