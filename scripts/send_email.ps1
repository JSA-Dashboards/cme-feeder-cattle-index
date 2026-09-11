# Send the daily FCI estimate by SMTP.
#
#     powershell -File scripts\send_email.ps1 -Preview      # write a .eml, do NOT send
#     powershell -File scripts\send_email.ps1               # send
#     powershell -File scripts\send_email.ps1 -Failed "update_exit=4 push_exit=0"
#
# WHY NOT OUTLOOK ANY MORE. This used Outlook COM so that no password had to be
# stored anywhere -- it borrowed the mail profile already authenticated on the
# machine. That reasoning was sound and is now moot: Ross runs the NEW Outlook
# (olk.exe), and Microsoft removed the COM automation interface from it
# entirely. There is no scripting hook to call. Classic OUTLOOK.EXE is still on
# disk and the Outlook.Application CLSID is still registered, which is the trap:
# COM happily resolves, tries to LAUNCH classic Outlook, and fails -- burning 60
# seconds every morning before giving up with CO_E_SERVER_EXEC_FAILURE.
#
# So: SMTP submission to Microsoft 365. Probed 2026-09-11 from this machine --
# smtp.office365.com:587 reachable, STARTTLS to TLS 1.3, and after the upgrade
# the server advertises "AUTH LOGIN XOAUTH2". jpsi.com is on Microsoft 365
# (MX -> jpsi-com.mail.protection.outlook.com), so this is first-party.
#
# THE PASSWORD. SMTP_PASSWORD goes in .env, which is gitignored. Use an APP
# PASSWORD, not the account password. If the tenant has SMTP AUTH disabled for
# the mailbox -- Microsoft turns it off by default -- the send fails with
# "5.7.139 ... SmtpClientAuthentication is disabled", and the fix is an admin
# enabling it for this one mailbox, or moving to Graph. The error is reported
# verbatim so that distinction is obvious rather than guessed at.
#
# Inert unless EMAIL_TO and the SMTP settings are present. Never fatal: a mail
# failure must not fail the pipeline, which has already done its real work.
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

# --- send it ----------------------------------------------------------------
$smtpHost = Read-EnvValue 'SMTP_HOST'; if (-not $smtpHost) { $smtpHost = 'smtp.office365.com' }
$smtpPort = Read-EnvValue 'SMTP_PORT'; if (-not $smtpPort) { $smtpPort = '587' }
$smtpUser = Read-EnvValue 'SMTP_USER'
$smtpPass = Read-EnvValue 'SMTP_PASSWORD'
$from     = Read-EnvValue 'EMAIL_FROM'; if (-not $from) { $from = $smtpUser }
# Preview runs before credentials exist, so fall back to the recipient purely
# so the previewed headers are not misleadingly blank.
if (-not $from) { $from = $to }

# Preview comes BEFORE the credential check on purpose: its whole job is to let
# you inspect the message, which is most useful precisely when SMTP is not
# working yet. Requiring credentials to preview would be backwards.
if ($Preview) {
    # No Outlook to open a draft in any more, so a preview writes the message to
    # a .eml file instead. Double-click it to see exactly what would go out.
    $eml = Join-Path $tmp 'preview.eml'
    @("From: $from", "To: $to", $(if ($cc) { "Cc: $cc" }), "Subject: $subject",
      'MIME-Version: 1.0', 'Content-Type: text/html; charset=utf-8', '', $body) |
        Where-Object { $_ -ne $null } | Set-Content -Path $eml -Encoding utf8
    Write-Output ("email: PREVIEW written to {0} - '{1}' to {2} (nothing sent)" -f $eml, $subject, $to)
    exit 0
}

if (-not $smtpUser -or -not $smtpPass) {
    Write-Output ('email: SMTP not configured - add SMTP_USER and SMTP_PASSWORD ' +
                  '(an APP PASSWORD, not your account password) to .env. Nothing sent.')
    exit 0
}

try {
    # TLS 1.2 minimum: PS 5.1 still defaults to SSL3/TLS1.0 on this build and
    # Microsoft 365 refuses those outright.
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

    $msg = New-Object Net.Mail.MailMessage
    $msg.From = New-Object Net.Mail.MailAddress($from)
    foreach ($a in ($to -split '[;,]')) { if ($a.Trim()) { $msg.To.Add($a.Trim()) } }
    if ($cc) { foreach ($a in ($cc -split '[;,]')) { if ($a.Trim()) { $msg.CC.Add($a.Trim()) } } }
    $msg.Subject = $subject
    $msg.Body = $body
    $msg.IsBodyHtml = $true

    $client = New-Object Net.Mail.SmtpClient($smtpHost, [int]$smtpPort)
    $client.EnableSsl = $true                  # STARTTLS on 587
    $client.Credentials = New-Object Net.NetworkCredential($smtpUser, $smtpPass)
    $client.Timeout = 30000
    $client.Send($msg)
    $msg.Dispose(); $client.Dispose()
    Write-Output ("email: sent '{0}' to {1}" -f $subject, $to)
} catch {
    # Report the server's own text. "5.7.139 SmtpClientAuthentication is
    # disabled" means the mailbox needs SMTP AUTH enabled and is NOT a bug here;
    # "5.7.57"/"535" means the credential is wrong, most often the account
    # password used where an app password is required.
    $m = $_.Exception.Message
    if ($_.Exception.InnerException) { $m += ' | ' + $_.Exception.InnerException.Message }
    Write-Output ("email: WARN send failed - {0}" -f $m)
}
exit 0
