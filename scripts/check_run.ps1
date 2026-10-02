# Did this morning's run do what it was supposed to?
#
#     powershell -NoProfile -ExecutionPolicy Bypass -File scripts\check_run.ps1
#     ... -Date 2026-09-10        # a specific day
#
# Answers, in order: did the task FIRE on schedule (rather than at boot or by
# hand), did WakeToRun actually wake the machine, did the pipeline finish and
# with what exit codes, did it freeze a morning call, and has CME printed the
# date it was estimating. Reads only -- changes nothing.
param([string]$Date = (Get-Date -Format 'yyyy-MM-dd'))

$repo = Split-Path -Parent $PSScriptRoot
$py   = Join-Path $repo '.venv\Scripts\python.exe'
$log  = Join-Path $repo ("logs\update_{0}.log" -f $Date)
$dayStart = [datetime]::ParseExact($Date, 'yyyy-MM-dd', $null)

# The morning Task Scheduler trigger, as hour and minute, in ONE place.
# Sections 2 and 3 both derive from it -- the WakeToRun band and the finish
# deadline -- and neither stores a clock time of its own. Both of the last two
# trigger moves left a baked-in literal behind here and turned a healthy run
# into a standing daily false alarm. Moving the trigger again is these two
# numbers and nothing else.
$trigH = 7; $trigM = 45    # 07:45 local. 07:30 -> 08:00 on 2026-09-29, 08:00 -> 07:45 on 2026-10-01.
function TriggerOn([datetime]$d) { $d.Date.AddHours($trigH).AddMinutes($trigM) }

function Head($t) { ''; '=== ' + $t + ' ===' }

Head "1. did Task Scheduler fire it, and how?"
$tsLog = Get-WinEvent -ListLog 'Microsoft-Windows-TaskScheduler/Operational' -ErrorAction SilentlyContinue
if (-not $tsLog.IsEnabled) {
    "   task history is DISABLED - cannot tell. Enable from an elevated shell:"
    "     wevtutil sl Microsoft-Windows-TaskScheduler/Operational /e:true"
} else {
    $ev = Get-WinEvent -LogName 'Microsoft-Windows-TaskScheduler/Operational' `
            -MaxEvents 400 -ErrorAction SilentlyContinue |
          Where-Object { $_.Message -like '*JSA FCI daily update*' -and
                         $_.TimeCreated -ge $dayStart -and
                         $_.TimeCreated -lt $dayStart.AddDays(1) }
    if (-not $ev) {
        "   NO task events for $Date. Either the task never fired, or history was"
        "   only enabled after it ran."
    } else {
        foreach ($e in ($ev | Sort-Object TimeCreated)) {
            $what = switch ($e.Id) {
                107 {'TRIGGERED ON SCHEDULE  <-- what we want'}
                118 {'triggered by BOOT (machine was off at trigger time)'}
                110 {'triggered BY A USER (run by hand)'}
                100 {'task started'} 102 {'task completed'} 129 {'python process created'}
                200 {'action started'} 201 {'action completed'}
                203 {'ACTION FAILED'} 111 {'task TERMINATED'}
                322 {'launch ignored - already running'}
                332 {'launch condition not met'}
                default {"event $($e.Id)"}
            }
            "   {0}  {1}" -f $e.TimeCreated.ToString('HH:mm:ss'), $what
        }
    }
}

Head "2. did WakeToRun wake the machine?"
$wake = Get-WinEvent -FilterHashtable @{LogName='System';
            ProviderName='Microsoft-Windows-Power-Troubleshooter'; Id=1} `
            -MaxEvents 8 -ErrorAction SilentlyContinue |
        Where-Object { $_.TimeCreated -ge $dayStart -and $_.TimeCreated -lt $dayStart.AddDays(1) }
# Only wakes anywhere near the 07:45 trigger tell us anything. Listing every
# wake in the day and flagging each as "not 07:45" is noise, and a check that
# cries wolf stops being read.
$near = @()
foreach ($w in $wake) {
    $x = [xml]$w.ToXml(); $d = @{}
    $x.Event.EventData.Data | ForEach-Object { $d[$_.Name] = $_.'#text' }
    $wt = [datetime]::Parse($d['WakeTime']).ToLocalTime()
    if ($wt.Hour -ge 5 -and $wt.Hour -lt 10) {
        $near += [pscustomobject]@{ Wake = $wt
            Slept = [datetime]::Parse($d['SleepTime']).ToLocalTime() }
    }
}
if (-not $wake) {
    "   no sleep/wake transition logged for $Date - the machine stayed awake, so"
    "   WakeToRun was not needed"
} elseif (-not $near) {
    "   the machine slept and woke on $Date, but not between 05:00 and 10:00 -"
    "   it was already awake at 07:45, so WakeToRun was not exercised"
} else {
    foreach ($n in $near) {
        "   woke {0}   (slept {1})" -f $n.Wake.ToString('HH:mm:ss'),
            $n.Slept.ToString('MM-dd HH:mm')
        # The trigger is 07:45, so 07:15 to 07:50 is the wake we asked for.
        # What this code stores is the two OFFSETS, -30 and +5; the clock times
        # come out of $trigH/$trigM at the top of the file.
        #
        # THE BAND IS DELIBERATELY LOPSIDED -- 30 minutes early, 10 late.
        #
        # THE +10 IS MEASURED, and the number it replaced was not. Windows'
        # Power-Troubleshooter events for the 26 mornings to 2026-10-02 put the
        # wake between +2.65 and +7.97 minutes after the trigger on 23 of them,
        # clustering +6 to +7. The three that are not -- 09-03 +24.95, 09-04
        # +28.82, 09-09 +15.30 -- are the real article, and 09-09 is the
        # 21-minute lag the deadline note below already cites.
        #
        # So there is an EMPTY GAP between 7.97 and 15.30, and +10 sits in it:
        # two minutes clear of the slowest ordinary wake, five clear of the
        # nearest genuine one. Do not re-tighten it to hug 7.97. The bound this
        # replaced was +5, which reported 'deferred' on 17 of those 26 mornings
        # -- 65% -- and the '+3 min' its comment claimed came from the only two
        # fast days on file (09-10 and 09-11, both +3.7). Nobody had compared it
        # against the power events until a 07:50:02 wake missed it by two
        # seconds on 2026-10-02.
        # An early wake is WakeToRun doing its job. A LATE one is the failure
        # this whole section exists to catch: the machine stayed asleep and a
        # human woke it, which is exactly what WakeToRun is supposed to make
        # unnecessary. Widening the late side hides the signal.
        #
        # This is the trap, and it has now been laid twice. The 07:30 version of
        # this test read `Hour -eq 7 -and Minute -le 35`, and that 35 was a LATE
        # bound sitting 5 minutes past a :30 trigger. Carrying the literal 35
        # across to an :00 trigger turns +5 into +35 and reports a half-hour
        # deferral as a success -- which is what the first pass at that edit did.
        # The 08:00 version then read 07:30..08:05, and carrying THAT 08:05
        # across to a 07:45 trigger would turn +5 into +20, while the 07:30 low
        # bound would tighten from -30 to -15 and start flagging a perfectly good
        # early wake. Derive both bounds or repeat the bug.
        $trigger = TriggerOn $n.Wake
        if ($n.Wake -ge $trigger.AddMinutes(-30) -and
            $n.Wake -le $trigger.AddMinutes(10)) {
            "     -> WakeToRun fired for the {0} trigger" -f $trigger.ToString('HH:mm')
        } else {
            "     -> woke at {0}, NOT {1} - the run was probably deferred until someone" -f $n.Wake.ToString('HH:mm'), $trigger.ToString('HH:mm')
            "        woke the machine, which is what WakeToRun exists to prevent"
        }
    }
}

Head "3. what does the pipeline's own log say?"
if (-not (Test-Path $log)) {
    "   NO LOG at $log - the script never started"
} else {
    Get-Content $log | Where-Object {
        $_ -match 'run started|run finished|FATAL|ERROR|WARN|healthcheck|Snowflake push|skipping' } |
      ForEach-Object { '   ' + $_ }
    # The FIRST run of the day is the morning call -- the one the 08:30 deadline
    # applies to. Taking the last would judge the afternoon pass (or a manual
    # evening run) against a deadline that was never meant for it.
    $started  = (Get-Content $log | Select-String 'run started'  | Select-Object -First 1)
    $finished = (Get-Content $log | Select-String 'run finished' | Select-Object -First 1)
    $nRuns = (Get-Content $log | Select-String 'run started').Count
    if ($nRuns -gt 1) { "   ({0} runs logged today; judging the FIRST against the deadline)" -f $nRuns }
    if ($started -and $finished) {
        $t0 = [datetime]::Parse((($started.Line -split '\s{2,}')[-1]).Trim())
        ''
        "   started {0}" -f $t0.ToString('HH:mm:ss')
        if ($finished.Line -match '(\d{2}:\d{2}:\d{2})\s*$') {
            $t1 = [datetime]::ParseExact($matches[1], 'HH:mm:ss', $null)
            "   finished {0}   (ran {1:n1} minutes)" -f $matches[1], ($t1 - $t0.Date.Add($t0.TimeOfDay)).TotalMinutes
            # The deadline is the TRIGGER plus the monitor's 45-minute grace --
            # 08:30 for a 07:45 trigger -- so this line and Healthchecks.io judge
            # the same instant. It is derived for the same reason the wake band
            # is: it was 08:15 while the trigger was 07:30, and moving the
            # trigger to 08:00 without moving this made the deadline
            # unreachable. 2026-09-30 started 08:00:03 and finished 08:25:00 with
            # every exit code 0, and would have been reported MISSED by 10
            # minutes. 14 of the 19 full runs on record would miss 08:15 from an
            # 08:00 start.
            #
            # Does 08:30 leave room? Over the 20 full morning runs from
            # 2026-09-12 (when the optional ingests landed) to 2026-10-01,
            # durations span 13.4 to 26.0 minutes, median 18.7. The last two,
            # 09-30 and 10-01, were both 25.0 -- high in that range since the
            # census and content check were added, but NOT a new worst: the
            # slowest on record is 26.0, on 2026-09-14. Taking 26.0 as the
            # bound. On time from 07:45 that is
            # 08:10-08:11, so 19-20 minutes spare. Two things eat into it:
            #   - a late wake. 07:50 is the latest the band above still calls
            #     healthy; a 26-minute run from there finishes 08:16, 14 clear.
            #   - the start lag. Every morning the machine had to be woken, the
            #     log shows the run starting 10-14 minutes after the trigger
            #     (07:39-07:44 off a 07:30 trigger) against 2-3 seconds on 09-30
            #     and 10-01 when it was already awake. Worst of those, 14 + 26,
            #     lands 08:25 and still reads MET.
            # Only the one-off 21-minute lag of 2026-09-09 on top of a record
            # run would breach, at 08:32. Healthchecks.io would NOT also be
            # alerting at that point: its cron still reads `0 8 * * *`, so it
            # waits until 08:45. The two agree only once the cron becomes
            # `45 7 * * *`; until then this line fires first. No wolf-crying
            # either way -- a healthy ~08:10 finish is quiet on both.
            #
            # CAVEAT, and it is the 2026-09-30 failure again from the other end:
            # that HC cron lives outside this repo and nothing here can move it.
            # While it still reads `0 8 * * *` it alerts at 08:45, so until it
            # becomes `45 7 * * *` this line is the stricter of the two by 15
            # minutes. It stays quiet on a healthy ~08:10 finish either way.
            $deadline = (TriggerOn $t0).AddMinutes(45)
            $done = $t0.Date.Add($t1.TimeOfDay)
            $dl = $deadline.ToString('HH:mm')
            if ($done -le $deadline) { "   MET the {0} deadline with {1:n0} min to spare" -f $dl, ($deadline - $done).TotalMinutes }
            else { "   MISSED the {0} deadline by {1:n0} min" -f $dl, ($done - $deadline).TotalMinutes }
        }
    } elseif ($started) {
        "   started but NEVER FINISHED - still running, or it died"
    }
}

Head "4. the data: frozen call, freshness, and CME's verdict"
if (Test-Path $py) {
    & $py (Join-Path $PSScriptRoot 'check_run.py') $Date
} else {
    "   venv python missing at $py"
}
''
