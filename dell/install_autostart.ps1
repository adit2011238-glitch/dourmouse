# install_autostart.ps1
# DOURMOUSE compute node (the Dell) - install the autostart scheduled task.
#
# The Dell is compute infrastructure, not DOURMOUSE. This registers a
# Scheduled Task that starts the node server after Windows boot, matching
# the documented setup in dell/README.md section 3. Fully reversible:
# run remove_autostart.ps1 to undo.
#
# Usage (Administrator PowerShell on the Dell):
#   powershell -ExecutionPolicy Bypass -File C:\dourmouse-node\install_autostart.ps1
#   powershell -ExecutionPolicy Bypass -File C:\dourmouse-node\install_autostart.ps1 `
#       -OllamaExe "C:\Program Files\Ollama\ollama.exe" -StartNow
#
# Options:
#   -NodeDir    deployment folder (default: C:\dourmouse-node)
#   -TaskName   scheduled task name (default: DOURMOUSE-ComputeNode)
#   -OllamaExe  also register a DOURMOUSE-Ollama task that runs
#               `ollama.exe serve` at boot - only needed if Ollama does
#               not autostart on its own
#   -StartNow   start the server task immediately after registering
#   -WhatIf     preview what would be registered without changing anything

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$NodeDir = "C:\dourmouse-node",
    [string]$TaskName = "DOURMOUSE-ComputeNode",
    [string]$OllamaExe = "",
    [switch]$StartNow
)

$ErrorActionPreference = "Stop"

function Fail([string]$Message) {
    Write-Host "ERROR: $Message" -ForegroundColor Red
    exit 1
}

# 1. Must run elevated: registering an AtStartup task requires admin.
$principal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())
$isAdmin = $principal.IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Fail "This script must run from an Administrator PowerShell. Right-click PowerShell and choose 'Run as administrator', then re-run: powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`""
}

# 2. Validate the node deployment before touching the scheduler.
$pyExe    = Join-Path $NodeDir ".venv\Scripts\python.exe"
$serverPy = Join-Path $NodeDir "dell_server.py"
foreach ($p in @($NodeDir, $pyExe, $serverPy)) {
    if (-not (Test-Path -LiteralPath $p)) {
        Fail "Missing: $p (deploy the node first - see dell/README.md section 1)"
    }
}

# 3. Build the task: run at startup as the logged-in user (RunLevel
#    Limited, no stored password), with auto-restart on failure and no
#    execution-time limit (the default 3-day kill is disabled so the
#    server runs indefinitely - it is built for long-running CPU
#    inference on 8 GB RAM).
$action   = New-ScheduledTaskAction -Execute $pyExe `
            -Argument "`"$serverPy`"" -WorkingDirectory $NodeDir
$trigger  = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
            -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
            -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
            -MultipleInstances IgnoreNew

if ($PSCmdlet.ShouldProcess($TaskName, "register the autostart scheduled task")) {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Description "DOURMOUSE compute node (LAN inference)" `
        -RunLevel Limited -Force | Out-Null
    Write-Host "Installed scheduled task: $TaskName"
}

# 4. Optional: Ollama itself. Registered as a SEPARATE task so a blocking
#    `ollama.exe serve` never delays the server task (task actions run in
#    sequence, so they must not share one task).
if ($OllamaExe) {
    if (-not (Test-Path -LiteralPath $OllamaExe)) {
        Fail "Ollama executable not found: $OllamaExe"
    }
    $ollamaTask = "DOURMOUSE-Ollama"
    if ($PSCmdlet.ShouldProcess($ollamaTask, "register the Ollama autostart task")) {
        $oAction   = New-ScheduledTaskAction -Execute $OllamaExe -Argument "serve" `
                     -WorkingDirectory (Split-Path -Parent $OllamaExe)
        $oSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
                     -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
                     -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
        Register-ScheduledTask -TaskName $ollamaTask -Action $oAction -Trigger $trigger `
            -Settings $oSettings -Description "DOURMOUSE Ollama engine (compute node)" `
            -RunLevel Limited -Force | Out-Null
        Write-Host "Installed scheduled task: $ollamaTask"
    }
}

# 5. Report state.
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    Write-Host ("Task state:        " + $task.State)
    Write-Host ("Runs at:           Windows startup (logged-in user, Limited)")
    if ($StartNow) {
        Start-ScheduledTask -TaskName $TaskName
        Write-Host "Started now."
    }
    Write-Host ""
    Write-Host "Verify from the MAIN computer: curl http://192.168.1.108:8000/v1/status"
    Write-Host ("Undo anytime: powershell -ExecutionPolicy Bypass -File " + $NodeDir + "\remove_autostart.ps1")
}
