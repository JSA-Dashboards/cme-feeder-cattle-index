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
# Only wakes anywhere near the 07:30 trigger tell us anything. Listing every
# wake in the day and flagging each as "not 07:30" is noise, and a check that
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
    "   it was already awake at 07:30, so WakeToRun was not exercised"
} else {
    foreach ($n in $near) {
        "   woke {0}   (slept {1})" -f $n.Wake.ToString('HH:mm:ss'),
            $n.Slept.ToString('MM-dd HH:mm')
        if ($n.Wake.Hour -eq 7 -and $n.Wake.Minute -le 35) {
            "     -> WakeToRun fired for the 07:30 trigger"
        } else {
            "     -> woke at {0}, NOT 07:30 - the run was probably deferred until someone" -f $n.Wake.ToString('HH:mm')
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
    # The FIRST run of the day is the morning call -- the one the 08:15 deadline
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
            $deadline = $t0.Date.AddHours(8).AddMinutes(15)
            $done = $t0.Date.Add($t1.TimeOfDay)
            if ($done -le $deadline) { "   MET the 08:15 deadline with {0:n0} min to spare" -f ($deadline - $done).TotalMinutes }
            else { "   MISSED the 08:15 deadline by {0:n0} min" -f ($done - $deadline).TotalMinutes }
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
