# install_desktop_autostart.ps1
# Register DOURMOUSE (the full app, not just a compute node) as a Scheduled
# Task on this desktop, so it runs detached from any login session and
# survives reboots.
#
# Why a Scheduled Task and not Start-Process: a process launched from an
# interactive session (including one opened over SSH) is tied to that
# session's console and is killed when the session ends, even when started
# with -WindowStyle Hidden. A Scheduled Task with -AtStartup runs outside
# any session and is the same pattern already used for the Dell
# (dell/install_autostart.ps1) - proven, and fully reversible.
#
# Usage (Administrator PowerShell):
#   powershell -ExecutionPolicy Bypass -File install_desktop_autostart.ps1 -WhatIf
#   powershell -ExecutionPolicy Bypass -File install_desktop_autostart.ps1 -StartNow

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$AppDir   = "C:\dourmouse",
    [string]$TaskName = "DOURMOUSE-Desktop",
    [string]$BindHost = "0.0.0.0",
    [switch]$StartNow
)

$ErrorActionPreference = "Stop"

function Fail([string]$m) { Write-Host "ERROR: $m" -ForegroundColor Red; exit 1 }

$principal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail "Run from an Administrator PowerShell - registering an AtStartup task requires it."
}

$pyExe = Join-Path $AppDir ".venv\Scripts\python.exe"
foreach ($p in @($AppDir, $pyExe, (Join-Path $AppDir "dourmouse"))) {
    if (-not (Test-Path -LiteralPath $p)) { Fail "Missing: $p" }
}

# Task actions cannot carry a persistent environment variable directly, so
# DOURMOUSE_HOST is set inline via cmd /c before the python invocation.
$action = New-ScheduledTaskAction -Execute "cmd.exe" `
    -Argument "/c set DOURMOUSE_HOST=$BindHost&& `"$pyExe`" -m dourmouse.webui >> `"$AppDir\server.log`" 2>> `"$AppDir\server.err.log`"" `
    -WorkingDirectory $AppDir

$trigger  = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -MultipleInstances IgnoreNew

if ($PSCmdlet.ShouldProcess($TaskName, "register the autostart scheduled task")) {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Description "DOURMOUSE host (desktop)" `
        -RunLevel Highest -Force | Out-Null
    Write-Host "Installed scheduled task: $TaskName"
}

if ($StartNow -and $PSCmdlet.ShouldProcess($TaskName, "start now")) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Started."
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    Write-Host ("Task state: " + $task.State)
    Write-Host "`nUndo:  Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
}
