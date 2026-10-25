$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
$statePath = Join-Path $root '.runtime\processes.json'

if (-not (Test-Path $statePath)) {
    Write-Host 'No runtime state was found. The app may already be stopped.' -ForegroundColor Yellow
    exit 0
}

$state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
foreach ($pidValue in @($state.frontend_pid, $state.backend_pid)) {
    if ($null -ne $pidValue -and (Get-Process -Id $pidValue -ErrorAction SilentlyContinue)) {
        & taskkill.exe /PID $pidValue /T /F 2>$null | Out-Null
    }
}

Remove-Item -LiteralPath $statePath -Force -ErrorAction SilentlyContinue
Write-Host 'AI Finance Assistant has stopped.' -ForegroundColor Green
