# install_desktop.ps1
# DOURMOUSE host setup for a Windows desktop (the always-on box).
#
# This machine runs DOURMOUSE ITSELF - app, UI, memory, orchestrator - not
# just a compute node. Inference is expected to be HOSTED (NVIDIA/OmniRoute),
# because a box without a capable GPU cannot serve agentic turns at usable
# speed (measured: 2.9 tok/s on the Dell = minutes per directive).
#
# Safe by construction:
#   - refuses to run without Administrator (firewall + power need it)
#   - -WhatIf previews every change without applying it
#   - never overwrites an existing .env value
#   - every system change it makes is listed at the end with its undo command
#
# Usage (Administrator PowerShell):
#   powershell -ExecutionPolicy Bypass -File C:\dourmouse\scripts\install_desktop.ps1 -WhatIf
#   powershell -ExecutionPolicy Bypass -File C:\dourmouse\scripts\install_desktop.ps1
#
# Options:
#   -AppDir     install folder                  (default: C:\dourmouse)
#   -Port       UI port                         (default: 8765)
#   -BindHost   listen address                  (default: 0.0.0.0)
#   -SkipVenv   do not create/populate the venv
#   -SkipPower  do not change sleep settings

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$AppDir   = "C:\dourmouse",
    [int]$Port        = 8765,
    [string]$BindHost = "0.0.0.0",
    [switch]$SkipVenv,
    [switch]$SkipPower
)

$ErrorActionPreference = "Stop"
$changes = New-Object System.Collections.ArrayList

function Say([string]$m)  { Write-Host $m }
function Ok([string]$m)   { Write-Host "  ok   $m" -ForegroundColor Green }
function Warn([string]$m) { Write-Host "  warn $m" -ForegroundColor Yellow }
function Fail([string]$m) { Write-Host "ERROR: $m" -ForegroundColor Red; exit 1 }
function Undo([string]$what, [string]$cmd) { [void]$changes.Add([pscustomobject]@{ Change = $what; Undo = $cmd }) }

# ---------------------------------------------------------------- 1. checks
$principal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail "Run this from an Administrator PowerShell (firewall and power settings require it)."
}

Say "`nDOURMOUSE desktop setup"
Say "  AppDir : $AppDir"
Say "  Bind   : ${BindHost}:$Port`n"

if (-not (Test-Path $AppDir)) {
    Fail "$AppDir does not exist. Copy the dourmouse-4.0.0 folder there first, then re-run."
}
foreach ($needed in @("dourmouse", "requirements.txt", "scripts")) {
    if (-not (Test-Path (Join-Path $AppDir $needed))) {
        Fail "$AppDir is missing '$needed' - that is not a complete dourmouse folder."
    }
}
Ok "app folder looks complete"

$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { Fail "Python not on PATH. Install Python 3.12 from python.org and tick 'Add to PATH'." }
$pyver = (& python --version 2>&1) -replace "Python ", ""
$major, $minor = $pyver.Split(".")[0, 1]
if ([int]$major -lt 3 -or ([int]$major -eq 3 -and [int]$minor -lt 11)) {
    Fail "Python $pyver is too old - need 3.11 or newer."
}
Ok "python $pyver"

if (-not (Get-Command chrome -ErrorAction SilentlyContinue) -and
    -not (Test-Path "C:\Program Files\Google\Chrome\Application\chrome.exe") -and
    -not (Test-Path "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe")) {
    Warn "Google Chrome not found - the browser agent drives real Chrome and will be degraded."
}

# ------------------------------------------------------------------ 2. venv
$venvPy = Join-Path $AppDir ".venv\Scripts\python.exe"
if ($SkipVenv) {
    Warn "skipping venv (-SkipVenv)"
} elseif (Test-Path $venvPy) {
    Ok "venv already present"
} elseif ($PSCmdlet.ShouldProcess("$AppDir\.venv", "create venv and install requirements")) {
    & python -m venv (Join-Path $AppDir ".venv")
    if (-not (Test-Path $venvPy)) { Fail "venv creation failed." }
    & $venvPy -m pip install --upgrade pip --quiet
    & $venvPy -m pip install -r (Join-Path $AppDir "requirements.txt")
    if ($LASTEXITCODE -ne 0) { Fail "pip install failed - see output above." }
    Ok "venv created and requirements installed"
    Undo "python venv at $AppDir\.venv" "Remove-Item -Recurse -Force '$AppDir\.venv'"
}

# ------------------------------------------------------------------- 3. env
# An access token is MANDATORY here: this host binds to the LAN, and with an
# empty token every route is unauthenticated (mail, files, shell). The app
# now refuses to start in that state rather than warning.
$envPath = Join-Path $AppDir ".env"
if (-not (Test-Path $envPath)) {
    Warn ".env not found - copy it from the Mac before starting the server."
} else {
    $envText  = Get-Content $envPath -Raw
    $hasToken = $envText -match "(?m)^DOURMOUSE_ACCESS_TOKEN=(.+)$"

    if (-not $hasToken -and $PSCmdlet.ShouldProcess($envPath, "append a generated DOURMOUSE_ACCESS_TOKEN")) {
        $bytes = New-Object byte[] 32
        [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
        $token = [Convert]::ToBase64String($bytes) -replace '[^A-Za-z0-9]', ''
        Copy-Item $envPath "$envPath.bak" -Force
        Add-Content $envPath "`r`nDOURMOUSE_ACCESS_TOKEN=$token"
        Ok "generated DOURMOUSE_ACCESS_TOKEN (backup at .env.bak)"
        Say "       token: $token"
        Say "       ^ save this - remote clients and the phone PWA need it"
        Undo "DOURMOUSE_ACCESS_TOKEN appended to .env" "Copy-Item '$envPath.bak' '$envPath' -Force"
    } elseif ($hasToken) {
        Ok "DOURMOUSE_ACCESS_TOKEN already set"
    }

    # Mac-specific paths break silently on Windows - surface them now.
    foreach ($key in @("ATLAS_REPO_PATH", "ATLAS_VENV_PATH", "OBSIDIAN_VAULT_PATH", "DOURMOUSE_WORKSPACE")) {
        if ($envText -match "(?m)^$key=(.+)$") {
            $val = $Matches[1].Trim()
            if ($val -and -not (Test-Path $val)) { Warn "$key points at a path that does not exist here: $val" }
        }
    }
    if ($envText -match "(?m)^DOURMOUSE_SERVER_URL=(.+)$" -and $Matches[1].Trim()) {
        Warn "DOURMOUSE_SERVER_URL is set - blank it unless the Dell still serves inference."
    }
}

# -------------------------------------------------------------- 4. firewall
$ruleName = "DOURMOUSE UI ($Port)"
if (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue) {
    Ok "firewall rule already present"
} elseif ($PSCmdlet.ShouldProcess($ruleName, "allow inbound TCP $Port on the Private profile only")) {
    New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow `
        -Protocol TCP -LocalPort $Port -Profile Private | Out-Null
    Ok "firewall: inbound TCP $Port allowed (Private profile ONLY)"
    Undo "firewall rule '$ruleName'" "Remove-NetFirewallRule -DisplayName '$ruleName'"
}
Warn "do NOT port-forward $Port on your router - this is a LAN service"

# ------------------------------------------------------------------ 5. power
# An always-on host must not sleep. Display off is free and does not matter.
if ($SkipPower) {
    Warn "skipping power settings (-SkipPower)"
} elseif ($PSCmdlet.ShouldProcess("power plan", "never sleep; display off after 10 min")) {
    & powercfg /change standby-timeout-ac 0
    & powercfg /change hibernate-timeout-ac 0
    & powercfg /change monitor-timeout-ac 10
    Ok "power: never sleep/hibernate on AC, display off after 10 min"
    Undo "sleep disabled on AC" "powercfg /change standby-timeout-ac 30"
}

# --------------------------------------------------------------- 6. verify
Say "`nRunning preflight...`n"
$preflight = Join-Path $AppDir "scripts\preflight.py"
if ((Test-Path $preflight) -and (Test-Path $venvPy)) {
    & $venvPy $preflight --host $BindHost --port $Port
    $pfCode = $LASTEXITCODE
} else {
    Warn "preflight not run (missing preflight.py or venv)"
    $pfCode = 0
}

# --------------------------------------------------------------- 7. summary
Say "`n---------------------------------------------------------------"
if ($changes.Count -gt 0) {
    Say "Changes made (each with its undo command):"
    foreach ($c in $changes) { Say "  * $($c.Change)`n      undo: $($c.Undo)" }
} else {
    Say "No changes made."
}

Say "`nStart the server with:"
Say "  `$env:DOURMOUSE_HOST='$BindHost'"
Say "  $venvPy -m dourmouse.webui"
Say "`nThen from another machine: http://<this-ip>:$Port"
Say "Remote clients must send the access token (Bearer header or cookie)."

if ($pfCode -ne 0) {
    Say "`nPreflight reported FAILures above - fix those before starting."
    exit 1
}
Say "`nSetup complete."
