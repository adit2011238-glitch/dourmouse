# remove_autostart.ps1
# DOURMOUSE compute node (the Dell) - remove the autostart scheduled task.
#
# Fully reversible uninstall matching dell/README.md section 3. By default
# only unregisters the scheduled task(s); nothing on disk is deleted. Add
# -RemoveNodeDir to also delete the deployment folder (destructive - the
# script refuses unless the folder contains dell_server.py).
#
# Usage (Administrator PowerShell on the Dell):
#   powershell -ExecutionPolicy Bypass -File C:\dourmouse-node\remove_autostart.ps1
#   powershell -ExecutionPolicy Bypass -File C:\dourmouse-node\remove_autostart.ps1 `
#       -RemoveOllamaTask -RemoveNodeDir
#
# Options:
#   -NodeDir          deployment folder (default: C:\dourmouse-node)
#   -TaskName         scheduled task name (default: DOURMOUSE-ComputeNode)
#   -RemoveOllamaTask also unregister the DOURMOUSE-Ollama task
#   -RemoveNodeDir    ALSO delete the deployment folder (destructive)
#   -WhatIf           preview without changing anything

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$NodeDir = "C:\dourmouse-node",
    [string]$TaskName = "DOURMOUSE-ComputeNode",
    [switch]$RemoveOllamaTask,
    [switch]$RemoveNodeDir
)

$ErrorActionPreference = "Stop"

function Fail([string]$Message) {
    Write-Host "ERROR: $Message" -ForegroundColor Red
    exit 1
}

# Unregistering a task also requires admin.
$principal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())
$isAdmin = $principal.IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Fail "This script must run from an Administrator PowerShell."
}

$tasks = @($TaskName)
if ($RemoveOllamaTask) { $tasks += "DOURMOUSE-Ollama" }

foreach ($name in $tasks) {
    $existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if (-not $existing) {
        Write-Host "Not installed (skipped): $name"
        continue
    }
    if ($PSCmdlet.ShouldProcess($name, "unregister the scheduled task")) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Write-Host "Removed scheduled task: $name"
    }
}

if ($RemoveNodeDir) {
    if (-not (Test-Path -LiteralPath $NodeDir)) {
        Write-Host "Node folder not present (skipped): $NodeDir"
    } elseif (-not (Test-Path -LiteralPath (Join-Path $NodeDir "dell_server.py"))) {
        Fail "$NodeDir does not look like the node folder (no dell_server.py) - refusing to delete"
    } elseif ($PSCmdlet.ShouldProcess($NodeDir, "delete the node folder")) {
        Remove-Item -LiteralPath $NodeDir -Recurse -Force
        Write-Host "Deleted node folder: $NodeDir"
    }
}

Write-Host "Done. The node no longer starts at boot."
