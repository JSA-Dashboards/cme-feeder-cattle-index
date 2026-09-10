# Send the daily FCI estimate through Outlook.
#
#     powershell -File scripts\send_email.ps1 -Preview      # open it, do NOT send
#     powershell -File scripts\send_email.ps1               # send
#     powershell -File scripts\send_email.ps1 -Failed "update_exit=4 push_exit=0"
#
# Outlook COM rather than SMTP, deliberately: it uses the mail profile already
# authenticated on this machine, so there is NO password stored in .env, no
# SMTP AUTH exemption to request from IT (Microsoft 365 disables basic auth by
# default), and no third-party mail service holding a key. The trade is that it
# needs an interactive session -- fine here, because the machine sleeps rather
# than shutting down and WakeToRun brings it back with the session intact.
#
# Inert unless EMAIL_TO is set in .env. Never fatal: a mail failure must not
# fail the pipeline, which has already done its real work by this point.
param(
    [switch]$Preview,
    [string]$Failed = '',
    [string]$Slot = ''
)

$repo = Split-Path -Parent $PSScriptRoot
$py   = Join-Path $repo '.venv\Scripts\python.exe'
$tmp  = Join-Path $repo '.tmp'

function Read-EnvValue([string]$name) {
    $envFile = Join-Path $repo '.env'
    if (-not (Test-Path $envFile)) { return $null }
    $m = Select-String -Path $envFile -Pattern ("^\s*{0}\s*=\s*(.+?)\s*$" -f $name) |
            Select-Object -First 1
    if ($m) { return $m.Matches[0].Groups[1].Value.Trim().Trim('"').Trim("'") }
    return $null
}

$to = Read-EnvValue 'EMAIL_TO'
$cc = Read-EnvValue 'EMAIL_CC'
if (-not $to) {
    Write-Output 'email: EMAIL_TO not set in .env - notification inert'
    exit 0
}

# --- build the content -------------------------------------------------------
$args = @('notify_email.py')
if ($Failed) { $args += @('--failed', $Failed) }
if ($Slot)   { $args += @('--slot', $Slot) }
try {
    Push-Location $repo
    & $py @args | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "notify_email.py exited $LASTEXITCODE" }
} catch {
    Write-Output ("email: could not build content - {0}" -f $_.Exception.Message)
    exit 0
} finally { Pop-Location }

$subjectFile = Join-Path $tmp 'email_subject.txt'
$bodyFile    = Join-Path $tmp 'email_body.html'
if (-not (Test-Path $subjectFile) -or -not (Test-Path $bodyFile)) {
    Write-Output 'email: content files missing after build - nothing sent'
    exit 0
}
$subject = (Get-Content $subjectFile -Raw -Encoding utf8).Trim()
$body    = Get-Content $bodyFile -Raw -Encoding utf8

# --- hand it to Outlook ------------------------------------------------------
# Outlook has to be RUNNING. If it is not, COM tries to launch it and can fail
# with 0x80080005 (CO_E_SERVER_EXEC_FAILURE) -- which is what happens from a
# sandboxed or non-desktop process. Start it ourselves first, minimised, and
# give it time to load the profile. On a normal morning Outlook is already open
# and this costs nothing.
# Returns nothing on purpose. The first version returned $true/$false and was
# called as `$null = Ensure-Outlook`, which in PowerShell discards the whole
# output stream -- so every diagnostic Write-Output inside it vanished too, and
# the 2026-09-10 failure logged only the bare COM error with no clue whether
# Outlook had been started. Status is not used by the caller anyway; the COM
# call below either works or does not.
function Ensure-Outlook {
    if (Get-Process OUTLOOK -ErrorAction SilentlyContinue) {
        Write-Output 'email: classic Outlook already running'
        return
    }
    # The NEW Outlook for Windows (olk.exe, the Microsoft.OutlookForWindows
    # store app) is a WebView2 wrapper around the web client and exposes NO COM
    # automation at all -- no Outlook.Application, no MAPI. If that is the mail
    # client in use, waiting 60s for classic to appear and then failing on COM
    # wastes a minute of every run and logs a misleading error. Say so and stop.
    if (Get-Process olk -ErrorAction SilentlyContinue) {
        Write-Output ('email: the NEW Outlook (olk.exe) is running, which has no COM ' +
                      'automation interface. Classic OUTLOOK.EXE is required for this ' +
                      'transport, or switch to SMTP/Graph -- see README-schedule.md.')
        return
    }
    Write-Output 'email: classic Outlook not running, starting it'
    try {
        Start-Process 'outlook.exe' -WindowStyle Minimized -ErrorAction Stop
    } catch {
        Write-Output ("email: could not start Outlook - {0}" -f $_.Exception.Message)
        return
    }
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 2
        if (Get-Process OUTLOOK -ErrorAction SilentlyContinue) {
            Write-Output ("email: Outlook started after {0}s, waiting for the profile" -f (($i + 1) * 2))
            Start-Sleep -Seconds 5
            return
        }
    }
    Write-Output 'email: Outlook did not start within 60s'
}

Ensure-Outlook
if ((Get-Process olk -ErrorAction SilentlyContinue) -and
    -not (Get-Process OUTLOOK -ErrorAction SilentlyContinue)) {
    Write-Output 'email: skipping the send - no COM-capable Outlook available'
    exit 0
}
try {
    $outlook = New-Object -ComObject Outlook.Application
    $mail = $outlook.CreateItem(0)            # 0 = olMailItem
    $mail.Subject = $subject
    $mail.To = $to
    if ($cc) { $mail.CC = $cc }
    $mail.HTMLBody = $body
    if ($Preview) {
        $mail.Display($false)                 # opens a window; sends nothing
        Write-Output ("email: PREVIEW opened in Outlook - '{0}' to {1}" -f $subject, $to)
    } else {
        $mail.Send()
        Write-Output ("email: sent '{0}' to {1}" -f $subject, $to)
    }
} catch {
    # Most likely causes, in order: no interactive session (a scheduled task
    # running with nobody logged on), Outlook blocked by antivirus programmatic
    # -access settings, or Outlook mid-upgrade. None are worth failing over.
    Write-Output ("email: WARN send failed - {0}" -f $_.Exception.Message)
}
exit 0
