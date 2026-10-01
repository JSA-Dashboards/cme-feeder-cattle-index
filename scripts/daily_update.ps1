# Daily FCI pipeline refresh. Registered in Task Scheduler as "JSA FCI daily update"
# (see scripts/README-schedule.md). Runs update_index.py against the repo's venv and
# appends stdout/stderr to logs/update_<date>.log.
#
# TWO TRIGGERS, one script. 07:45 local is the morning call -- the number that goes
# out and the one comparable to CIH's and Compass's morning sheets. 13:00 is the
# settled pass, and it exists because USDA publication is not finished by then:
# measured 2026-09-09 over 80 auctions and 246 reports, 85.2% of a sale day's
# qualifying head is fetchable by 07:30 the next morning, but 95.8% by noon. Nearly
# all of that gap is OKC West (El Reno), which publishes its previous-day sale at a
# median of +1 day 11:13 and had missed the morning run 7 times out of 7; folding its
# 09/08 sale in moved that date's estimate +0.33, about 80x the scorecard's MAE.
# Those shares were measured at 07:30. The morning trigger has moved twice since:
# 07:30 -> 08:00 on 2026-09-29, and 08:00 -> 07:45 on 2026-10-01 so the estimate is
# in hand before the morning email goes out. Neither move touches the gap the
# afternoon pass closes -- the coverage table in README-schedule.md reads the same
# 51,503 head at 07:30 and at 08:00, and 07:45 lies between them, so it cannot
# differ either.
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
# ONE CHECK PER SLOT. The original reason was arithmetic: a five-field cron
# shares a single minute field, and with the runs at 07:30 and 13:00 there was
# no way to express both in one -- "30 7,13 * * *" means 07:30 and 13:30, and
# would have cried wolf every afternoon. That constraint lapsed for one day at
# 08:00 (2026-09-29), when both slots sat on minute 0 and "0 8,13 * * *" would
# have expressed them together -- and it came straight BACK on 2026-10-01 with
# the move to 07:45: the minute fields differ again (45 and 0), so "45 7,13 * * *"
# means 07:45 and 13:45 and a single five-field cron genuinely cannot describe
# both runs. The two checks were kept through the one day they were optional,
# and that is why nothing had to be rebuilt now. Keeping them is also right on
# its own merits -- one per slot keeps a missed morning's alert and history
# separate from a missed afternoon's, and a combined check would go red without
# saying which run was lost. Each has its own cron ("45 7 * * *" and
# "0 13 * * *"), and this script pings whichever one matches the slot it is
# running in.
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
        Log (("WARN: CME pull failed (exit {0}). Continuing - the reconstruction and " +
              "publish are unaffected, but 'Last CME Print' will stay stale.") -f $cmeCode)
    }
}

# The missing-barn report, and it belongs HERE rather than inside update_index.py.
# barn_report names the index date derived from MAX(cme_ftp_daily), so run from
# the ingest -- which finishes before the pull above -- it read a value one print
# stale and named YESTERDAY'S index date every morning. On 2026-09-25 it printed
# "index date 2026-09-23" while the number being published was 2026-09-24.
# Print-only and guarded on both sides, so it cannot fail the run: its exit code
# is logged and deliberately not tested.
$barnCode = Invoke-Py @('update_index.py', '--barn-report-only') 'barn'
if ($barnCode -ne 0) {
    Log ("WARN: barn report failed (exit {0}). The index and the push are unaffected." -f $barnCode)
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
    } elseif ($pushCode -eq 3) {
        # Exit 3 is "loaded, but the contents do not match local SQLite" and it
        # needs its own sentence. The generic message below says Snowflake still
        # holds its previous contents, which is true when a transaction rolled
        # back and FALSE here -- the write COMMITTED. Reusing it would print a
        # reassuring untruth at the exact moment somebody is reading the log to
        # understand a live incident. See 02_migrate_data.py's exit codes.
        Log (("ERROR: the index push COMMITTED but CONTENT VERIFICATION FAILED " +
              "(exit 3). This is NOT the stale-dashboard case: Snowflake holds " +
              "NEW data that disagrees with local SQLite, so the DASHBOARD MAY " +
              "BE SERVING WRONG VALUES. The differing tables and columns are in " +
              "the push output above - read them before re-pushing. This is the " +
              "shape of the 2026-09-29 incident."))
    } else {
        Log (("ERROR: index push failed (exit {0}). Local SQLite is current but " +
              "the DASHBOARD IS STALE - each table rolls back individually, so " +
              "Snowflake still holds its previous contents.") -f $pushCode)
    }
} else {
    Log 'skipping Snowflake push: the USDA refresh failed, nothing good to publish'
}

# -- Daily estimate email -----------------------------------------------------
# SENT HERE, immediately after the index is published, since 2026-10-01. It used
# to be the very last thing the script did, which cost the morning mail about
# twelve minutes for nothing: it waited behind the border, census, calf, corn and
# herd ingests and the dashboard push, not one of which it reads.
#
# WHY IT IS SAFE AT THIS LINE. notify_email.py queries four tables and no others
# -- cme_ftp_daily, fci_daily, fci_snapshots, peer_estimates -- and all four are
# in CRITICAL_TABLES in snowflake/02_migrate_data.py, so the --critical-only push
# above has already committed every row the mail can see. Everything below here
# is dashboard data: a stale tab, never the number.
#
# WHAT DID NOT MOVE, AND MUST NOT. The healthcheck ping stays at the END of this
# script. The guarantee against a silent failure is the ABSENCE of a ping, not
# the /fail line: the 13:00 run that hung on 2026-09-10 was killed at its time
# limit, and a killed process never reaches any line it has not got to yet. Move
# the SUCCESS ping up here and a hang in the ingests below would be pre-announced
# as a success -- the monitor satisfied, the alert never fired, and the exact
# hole the dead-man's switch was built to close re-opened. The email may move
# because it REPORTS; the ping may not because it CERTIFIES. The ingests do not
# move either, for the reason recorded above the push: third-party API calls stay
# off the index's critical path.
#
# ONE BEHAVIOUR CHANGE, chosen rather than inherited. If the run hangs below,
# Ross now gets the estimate where before he got nothing at all, and the monitor
# still goes DOWN on the missing ping. So a single morning can now produce a
# normal-looking estimate email AND a DOWN alert. They do not contradict each
# other: the mail says "the index for this date is published", true the instant
# the push above returned, and the alert says "the run never reported finishing",
# also true. Different halves of one job. NO marker is added to the mail to hedge
# this, and that is a decision, not an oversight -- nothing has failed at this
# line and nothing here can know whether anything will, so a caveat would print
# every morning and be wrong on all but the rare one, which is the same cry-wolf
# argument that keeps the afternoon pass silent. If a marker is ever wanted it
# belongs in notify_email.py's footer, worded once, not in this dispatch.
#
# THE SLOT DOES NOT CHANGE, and that was checked rather than assumed, because
# whether any mail is sent at all hangs on it. snapshots.run_slot() splits on
# hour 11 (AM_PM_BOUNDARY_HOUR). The two logged 25.0-minute runs under the 08:00
# trigger (2026-09-30 and 2026-10-01) mailed at 08:25 and would now mail about
# 08:13; the same run under the 07:45 trigger finishes about 08:10 and will now
# mail about 07:58. Every one of those is hour 7 or 8, nowhere near 11, so the
# morning still mails and the afternoon pass still stays silent. It also reads
# the clock closer to $slotNow at the top of the script, which applies the same
# 11:00 boundary about twenty-five minutes earlier -- two readings that could in
# principle disagree, and now have less room to.
#
# THE MORNING RUN MAILS THE ESTIMATE; the afternoon pass stays silent unless it
# broke. Two identical-looking emails a day trains you to ignore both, and the
# afternoon number is a refinement rather than news. A FAILURE always mails, from
# either slot, because that is the case worth interrupting someone for -- and it
# now leaves twelve minutes sooner too.
#
# $mailSummary is NOT the $summary the healthcheck sends at the end, and the two
# must never be merged back into one variable. This one is stamped when the
# PUBLISH STEP ended; that one is stamped when the RUN finished, and those are
# now about twelve minutes apart. Each is true where it is built; sharing one
# string would silently put the wrong clock on one of them and nobody would
# notice which.
#
# It says publish_step= and not published= on purpose. The only branch that ever
# sends $mailSummary is the FAILURE branch below, so the one place a human reads
# this string is a failure email -- where "published=08:13 push_exit=1" would be
# a flat untruth sitting next to the exit code that contradicts it. Caught by
# running the stubbed failure path, not by reading it.
#
# About those twelve minutes: logs/update_*.log stamps only the run's start and
# finish, so the figure is a difference of days rather than a measurement. The
# herd block cost 3.9 min across the 2026-09-25/09-26 boundary (15.1 -> 19.0 min
# median) and update_imports cost 7.0 min across 09-10/09-11 (5.0 -> 12.0), and
# it has grown since with calf, corn and feed. The stamp this block logs below
# turns that inference into a measurement from tomorrow: subtract it from the
# "run finished" stamp and you have the real tail.
#
# Delivery is scripts/send_email.ps1 -- a OneDrive drop that a Power Automate
# flow sends from Ross's own identity, with SMTP as a fallback for a mailbox that
# ever permits it. NOT Outlook COM: that was the original design and it died with
# the new Outlook (olk.exe), which has no COM interface at all; the comment that
# used to sit here still claimed it. Inert without EMAIL_TO, and never fatal --
# by this line the pipeline has already done its real work.
$mailSummary = ("update_exit={0} cme_exit={1} push_exit={2} publish_step={3}" -f `
                $code, $cmeCode, $pushCode, (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
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
        -File (Join-Path $repo 'scripts\send_email.ps1') -Failed $mailSummary |
        ForEach-Object { Log $_ }
}
# Stamped on purpose. Compare it with the "run finished" stamp at the bottom and
# the log itself tells you how long the mail no longer waits -- the one number
# this move was made for, which until now could only be inferred across days.
Log ("estimate email step done  {0}" -f (Get-Date -Format 'HH:mm:ss'))

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
        Log (("WARN: import refresh failed (exit {0}). Continuing - the FCI " +
              "estimate is unaffected; the supply-side tabs will be stale.") -f $impCode)
    }
}

# The two US Cow Herd feeds. Only that one dashboard reads them, so a failure
# here is one stale page and nothing else -- the push below is deliberately NOT
# gated on them, the same way it is not gated on the index push.
#
# replacement_reports.py HAD NO CALLER AT ALL until 2026-09-25. It was run by
# hand the day the page was built and never again, so replacement_sales sat
# frozen at 2026-09-10 while 02_migrate_data.py faithfully re-uploaded the same
# 73,156 rows to Snowflake every night. That is the failure mode worth
# remembering: the table was written daily, so any check asking "was this
# refreshed today" passed the whole time. Only the CONTENT was stale.
$herdCode = 0
if ($code -eq 0) {
    Log '--- refreshing the herd sources (replacement sales, feeder sex mix) ---'
    $repCode = Invoke-Py @('replacement_reports.py') 'rep'
    if ($repCode -ne 0) {
        Log (("WARN: replacement-report refresh failed (exit {0}). The retention " +
              "incentive on the US Cow Herd page will be stale.") -f $repCode)
        $herdCode = $repCode
    }
    $fsmCode = Invoke-Py @('feeder_sex_mix.py') 'fsm'
    if ($fsmCode -ne 0) {
        Log (("WARN: feeder sex-mix refresh failed (exit {0}). The heifer-share " +
              "section will be stale.") -f $fsmCode)
        $herdCode = $fsmCode
    }
}

$optCode = 0
if ($code -eq 0 -and $impCode -eq 0) {
    Checkpoint-Wal
    Log '--- pushing the dashboard tables to Snowflake ---'
    $optCode = Invoke-Py @('snowflake/02_migrate_data.py', '--optional-only') 'opt'
    if ($optCode -ne 0) {
        Log (("WARN: dashboard push failed (exit {0}). The index published " +
              "normally; the supply-side tabs will be stale.") -f $optCode)
    }
}

Log ("run finished  update_exit={0}  cme_exit={1}  push_exit={2}  imp_exit={3}  herd_exit={4}  opt_push_exit={5}  {6}" -f $code, $cmeCode, $pushCode, $impCode, $herdCode, $optCode, (Get-Date -Format 'HH:mm:ss'))

# Prune logs older than 30 days so this doesn't grow without bound.
Get-ChildItem $logDir -Filter 'update_*.log' -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } |
    Remove-Item -Force -ErrorAction SilentlyContinue

# Tell the monitor how it went. A partial success still counts as a FAILURE
# here: if the Snowflake push did not land, the dashboard is stale, and that is
# precisely the silent failure this is meant to surface -- the local run having
# "worked" is no comfort to someone reading the page.
#
# THIS IS THE LAST THING THE SCRIPT DOES, and it has to stay that way. The
# estimate email moved up to the critical push on 2026-10-01; the ping did not,
# because the monitor's guarantee is the ABSENCE of this ping. Send it before
# the optional ingests and a hang in them -- the 2026-09-10 failure, killed at
# its time limit -- would arrive as a green check. See the long note at the
# email block for the full argument.
#
# $summary is stamped FINISHED and is deliberately a DIFFERENT string from the
# $mailSummary built at the email block, which is stamped at the end of the
# PUBLISH STEP. They are about twelve minutes apart now. Do not collapse them
# into one variable to save
# a line: whichever call site inherited the other's clock would go on reporting
# a plausible, wrong time forever.
$summary = ("update_exit={0} cme_exit={1} push_exit={2} finished={3}" -f `
            $code, $cmeCode, $pushCode, (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
if ($code -eq 0 -and $pushCode -eq 0) {
    Ping-Health '' $summary
} else {
    Ping-Health '/fail' $summary
}

# Distinct exit codes so Task Scheduler's LastTaskResult says WHICH half failed:
# the update itself, or only the publish step.
if ($code -ne 0)      { exit $code }
elseif ($pushCode -ne 0) { exit 5 }
else                  { exit 0 }
