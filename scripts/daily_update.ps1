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

# -- Dead-man's switch --------------------------------------------------------
# HEALTHCHECK_URL is an opaque capability URL from an external monitor
# (healthchecks.io free tier is enough). It has to be EXTERNAL, and that is the
# whole point: nothing running on this desktop can report that this desktop is
# asleep, powered off, or that Task Scheduler never fired at all. The monitor
# alerts on the ABSENCE of a ping, so silence is the signal and no cooperation
# from the failing component is required.
#
# /start on begin, the bare URL on success, /fail on a failure -- so an alert
# can distinguish "never ran" from "ran and broke" and carry the exit codes.
# The /fail pings are a courtesy only; the guarantee comes from absence.
#
# Every ping is best-effort and logged, never fatal: an outage at the monitor,
# or no URL configured at all, must not stop the pipeline. Unset = inert.
# ONE CHECK PER SLOT, because a five-field cron shares a single minute field
# and this job runs at 07:30 and 13:00 -- "30 7,13 * * *" would expect 07:30 and
# 13:30 and cry wolf every afternoon. So the monitor gets two checks, each with
# its own cron ("30 7 * * *" and "0 13 * * *"), and this script pings whichever
# one matches the slot it is running in.
#
# Monitoring BOTH matters: the failure that prompted all this was the 13:00 run
# hanging and being killed on 2026-09-10. A killed process never reaches its
# /fail line, so absence is the only signal, and a morning-only check would have
# stayed green through it.
#
# HEALTHCHECK_URL (unsuffixed) is still honoured as a single check for both
# slots, so an existing setup keeps working.
$slotNow = if ((Get-Date).Hour -lt 11) { 'am' } else { 'pm' }   # same 11:00 boundary as snapshots.run_slot()

function Get-EnvValue([string]$Name) {
    $envFile = Join-Path $repo '.env'
    if (-not (Test-Path $envFile)) { return $null }
    $m = Select-String -Path $envFile -Pattern ('^\s*' + [regex]::Escape($Name) + '\s*=\s*(\S+)') |
            Select-Object -First 1
    if ($m) { return $m.Matches[0].Groups[1].Value.Trim().Trim('"').Trim("'") }
    return $null
}

$hcUrl = Get-EnvValue ('HEALTHCHECK_URL_' + $slotNow.ToUpper())
if (-not $hcUrl) { $hcUrl = Get-EnvValue 'HEALTHCHECK_URL' }

function Ping-Health {
    param([string]$Suffix = '', [string]$Body = '')
    if (-not $hcUrl) { return }
    $label = if ($Suffix) { $Suffix.TrimStart('/') } else { 'success' }
    try {
        # PS 5.1 does not negotiate TLS 1.2 by default on this build; without
        # this the request fails with an opaque "could not create SSL/TLS
        # secure channel".
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri ($hcUrl.TrimEnd('/') + $Suffix) -Method Post `
            -Body $Body -UseBasicParsing -TimeoutSec 10 -ErrorAction Stop | Out-Null
        Log ("healthcheck: {0} ping sent" -f $label)
    } catch {
        Log ("WARN: healthcheck {0} ping failed - {1}" -f $label, $_.Exception.Message)
    }
}

# Start a python step, wait for it, and fold its output into the log. The same
# redirect-to-temp-files dance appeared three times before the reorder needed a
# fourth, and PS 5.1's handling of a native exe's stderr (see the note above
# $outFile) is subtle enough that it should be written down once.
function Invoke-Py {
    param([string[]]$Arguments, [string]$Tag)
    $o = Join-Path $env:TEMP ('fci_{0}_out_{1}.txt' -f $Tag, $PID)
    $e = Join-Path $env:TEMP ('fci_{0}_err_{1}.txt' -f $Tag, $PID)
    try {
        $pr = Start-Process -FilePath $py -ArgumentList (@('-u') + $Arguments) `
                -WorkingDirectory $repo -NoNewWindow -Wait -PassThru `
                -RedirectStandardOutput $o -RedirectStandardError $e
        $rc = $pr.ExitCode
    } catch {
        Log ("WARN: could not start {0} - {1}" -f $Tag, $_.Exception.Message)
        $rc = 8
    }
    foreach ($f in @($o, $e)) {
        if (Test-Path $f) {
            $c = Get-Content $f | Where-Object { $_ -ne '' }
            if ($c) { $c | ForEach-Object { Log $_ } }
            Remove-Item $f -Force
        }
    }
    return $rc
}

# Fold any WAL contents back into the .db before a push reads it. update_index.py
# normally checkpoints on close, but an interrupted run can leave rows stranded
# in data/mars_history.db-wal, and the push would then upload a dataset missing
# that day's rows. Needed before BOTH pushes now, for the same reason.
function Checkpoint-Wal {
    try {
        & $py -c "import sqlite3,os; d=os.path.join(r'$repo','data','mars_history.db'); c=sqlite3.connect(d); c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); print('wal checkpointed; fci rows =', c.execute('select count(*) from fci_daily').fetchone()[0]); c.close()" 2>&1 |
            ForEach-Object { Log $_ }
    } catch {
        Log ("WARN: wal checkpoint failed - {0}" -f $_.Exception.Message)
    }
}

if ($hcUrl) { Log ("healthcheck: pinging the {0} check" -f $slotNow); Ping-Health '/start' }
else { Log ("healthcheck: no HEALTHCHECK_URL_{0} or HEALTHCHECK_URL in .env, monitoring inert" -f $slotNow.ToUpper()) }

if (-not (Test-Path $py))                    { Log 'FATAL: venv python missing'; Ping-Health '/fail' 'venv python missing'; exit 2 }
if (-not (Test-Path (Join-Path $repo '.env'))) { Log 'FATAL: .env missing - MARS_API_KEY unavailable'; exit 3 }   # no .env means no URL to ping; absence is the alert

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

# ORDER MATTERS HERE, and it was wrong until 2026-09-11: the optional ingests
# now run AFTER the index is published, not before.
#
# update_imports.py takes about eight minutes -- calf_sales alone re-walks the
# full auction roster that update_index.py just walked (409s measured) and
# corn_bids adds 52s. Ahead of the push, every one of those minutes sat between
# a finished index and a published one. Worse, it put third-party API calls on
# the critical path: the 13:00 run hung and was killed on 2026-09-10, and a hang
# in that position would now strand a perfectly good index unpublished.
#
# So: publish the index, then ingest, then push what the ingest produced. The
# CRITICAL/OPTIONAL split that makes this possible lives in 02_migrate_data.py
# rather than in an array here, so adding a table cannot silently leave it out.
$pushCode = 0
if ($code -eq 0) {
    Checkpoint-Wal
    Log '--- pushing the index to Snowflake (JSA.CME_FEEDER_CATTLE) ---'
    $pushCode = Invoke-Py @('snowflake/02_migrate_data.py', '--critical-only') 'push'
    if ($pushCode -eq 0) {
        Log 'index push OK - dashboard is serving current data'
    } else {
        Log ("ERROR: index push failed (exit {0}). Local SQLite is current but " +
             "the DASHBOARD IS STALE - each table rolls back individually, so " +
             "Snowflake still holds its previous contents." -f $pushCode)
    }
} else {
    Log 'skipping Snowflake push: the USDA refresh failed, nothing good to publish'
}

# Everything past this point is dashboards, not the index, and is NON-FATAL
# throughout: none of it feeds the FCI estimate, which is already published
# above. The worst case is a stale tab.
#
# Deliberately NOT gated on $pushCode. A Snowflake outage during the index push
# is no reason to also skip collecting today's border, calf and corn data --
# that data is only published once, and the local ingest is what makes tomorrow
# able to catch up.
$impCode = 0
if ($code -eq 0) {
    Log '--- refreshing the supply-side sources (border, census, calf, corn) ---'
    $impCode = Invoke-Py @('update_imports.py') 'imp'
    if ($impCode -ne 0) {
        Log ("WARN: import refresh failed (exit {0}). Continuing - the FCI " +
             "estimate is unaffected; the supply-side tabs will be stale." -f $impCode)
    }
}

$optCode = 0
if ($code -eq 0 -and $impCode -eq 0) {
    Checkpoint-Wal
    Log '--- pushing the dashboard tables to Snowflake ---'
    $optCode = Invoke-Py @('snowflake/02_migrate_data.py', '--optional-only') 'opt'
    if ($optCode -ne 0) {
        Log ("WARN: dashboard push failed (exit {0}). The index published " +
             "normally; the supply-side tabs will be stale." -f $optCode)
    }
}

Log ("run finished  update_exit={0}  cme_exit={1}  push_exit={2}  imp_exit={3}  opt_push_exit={4}  {5}" -f $code, $cmeCode, $pushCode, $impCode, $optCode, (Get-Date -Format 'HH:mm:ss'))

# Prune logs older than 30 days so this doesn't grow without bound.
Get-ChildItem $logDir -Filter 'update_*.log' -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } |
    Remove-Item -Force -ErrorAction SilentlyContinue

# Tell the monitor how it went. A partial success still counts as a FAILURE
# here: if the Snowflake push did not land, the dashboard is stale, and that is
# precisely the silent failure this is meant to surface -- the local run having
# "worked" is no comfort to someone reading the page.
$summary = ("update_exit={0} cme_exit={1} push_exit={2} finished={3}" -f `
            $code, $cmeCode, $pushCode, (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
if ($code -eq 0 -and $pushCode -eq 0) {
    Ping-Health '' $summary
} else {
    Ping-Health '/fail' $summary
}

# -- Daily estimate email -----------------------------------------------------
# The MORNING run mails the estimate; the afternoon pass stays silent unless it
# broke. Two identical-looking emails a day trains you to ignore both, and the
# afternoon number is a refinement rather than news. A FAILURE always mails,
# from either slot, because that is the case worth interrupting someone for.
#
# Sent through Outlook COM (see send_email.ps1) so no mail password is stored
# anywhere. Inert unless EMAIL_TO is set in .env, and never fatal -- by this
# point the pipeline has already done its real work and published.
$slot = ''
try { $slot = (& $py -c "import sys; sys.path.insert(0, r'$repo'); from snapshots import run_slot; print(run_slot())" 2>$null).Trim() } catch { }
if ($code -eq 0 -and $pushCode -eq 0) {
    if ($slot -eq 'am') {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass `
            -File (Join-Path $repo 'scripts\send_email.ps1') -Slot $slot |
            ForEach-Object { Log $_ }
    } else {
        Log ("email: {0} slot succeeded - no mail sent by design" -f ($(if ($slot) { $slot } else { 'unknown' })))
    }
} else {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass `
        -File (Join-Path $repo 'scripts\send_email.ps1') -Failed $summary |
        ForEach-Object { Log $_ }
}

# Distinct exit codes so Task Scheduler's LastTaskResult says WHICH half failed:
# the update itself, or only the publish step.
if ($code -ne 0)      { exit $code }
elseif ($pushCode -ne 0) { exit 5 }
else                  { exit 0 }
