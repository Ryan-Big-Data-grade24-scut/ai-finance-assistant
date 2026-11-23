$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/app-processes.ps1"

$lock = $null
$failed = $false
try {
    $lock = Lock-App
    foreach ($name in 'frontend', 'backend') {
        try { Stop-AppProcess $name (Read-AppRecord $name) } catch {
            Write-Warning $_.Exception.Message
            $failed = $true
        }
    }
} catch {
    Write-Warning $_.Exception.Message
    $failed = $true
} finally {
    if ($lock) { $lock.Dispose() }
}

if ($failed) { exit 1 }
Write-Host 'No one-click app processes remain in this directory.' -ForegroundColor Green
