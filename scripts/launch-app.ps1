param(
    [int]$BackendPort = 8000,
    [int]$FrontendPort = 5173,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$frontendDir = Join-Path $root 'frontend'
$runtimeDir = Join-Path $root '.runtime'
$statePath = Join-Path $runtimeDir 'processes.json'

function Test-Endpoint([string]$Url) {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 2
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 500
    } catch {
        return $false
    }
}

function Wait-Endpoint([string]$Name, [string]$Url, $Process, [int]$Seconds = 30) {
    for ($i = 0; $i -lt $Seconds; $i++) {
        if (Test-Endpoint $Url) {
            Write-Host "  [OK] $Name is ready" -ForegroundColor Green
            return $true
        }
        if ($null -ne $Process -and $Process.HasExited) {
            return $false
        }
        Start-Sleep -Seconds 1
    }
    return $false
}

function Show-LogTail([string]$Path) {
    if (Test-Path $Path) {
        Write-Host "`n  Recent log: $Path" -ForegroundColor Yellow
        Get-Content -LiteralPath $Path -Tail 30
    }
}

function Stop-StartedProcess($Process) {
    if ($null -ne $Process -and -not $Process.HasExited) {
        & taskkill.exe /PID $Process.Id /T /F 2>$null | Out-Null
    }
}

if (-not (Test-Path $python)) {
    throw 'Missing .venv. Run scripts\setup.ps1 first.'
}
if (-not (Test-Path (Join-Path $frontendDir 'node_modules'))) {
    throw 'Missing frontend dependencies. Run scripts\setup.ps1 first.'
}

$npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
if (-not $npm) {
    throw 'npm.cmd was not found. Install Node.js and try again.'
}

New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backendOut = Join-Path $runtimeDir "backend-$stamp.log"
$backendErr = Join-Path $runtimeDir "backend-$stamp-error.log"
$frontendOut = Join-Path $runtimeDir "frontend-$stamp.log"
$frontendErr = Join-Path $runtimeDir "frontend-$stamp-error.log"
$backendProcess = $null
$frontendProcess = $null

try {
    Write-Host ''
    Write-Host 'Starting AI Finance Assistant...' -ForegroundColor Cyan

    if (Test-Endpoint "http://127.0.0.1:$BackendPort/health") {
        Write-Host "  [OK] Backend is already running on port $BackendPort" -ForegroundColor Green
    } else {
        Write-Host '  Starting backend...' -ForegroundColor Cyan
        $backendProcess = Start-Process -FilePath $python `
            -ArgumentList @('-m', 'uvicorn', 'backend.main:app', '--host', '127.0.0.1', '--port', "$BackendPort") `
            -WorkingDirectory $root -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput $backendOut -RedirectStandardError $backendErr
        if (-not (Wait-Endpoint 'Backend' "http://127.0.0.1:$BackendPort/health" $backendProcess)) {
            Show-LogTail $backendErr
            throw 'Backend failed to start.'
        }
    }

    if (Test-Endpoint "http://127.0.0.1:$FrontendPort") {
        Write-Host "  [OK] Frontend is already running on port $FrontendPort" -ForegroundColor Green
    } else {
        Write-Host '  Starting frontend...' -ForegroundColor Cyan
        $frontendProcess = Start-Process -FilePath $npm.Source `
            -ArgumentList @('run', 'dev', '--', '--host', '127.0.0.1', '--port', "$FrontendPort") `
            -WorkingDirectory $frontendDir -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput $frontendOut -RedirectStandardError $frontendErr
        if (-not (Wait-Endpoint 'Frontend' "http://127.0.0.1:$FrontendPort" $frontendProcess)) {
            Show-LogTail $frontendErr
            Show-LogTail $frontendOut
            throw 'Frontend failed to start.'
        }
    }

    [ordered]@{
        backend_pid = if ($backendProcess) { $backendProcess.Id } else { $null }
        frontend_pid = if ($frontendProcess) { $frontendProcess.Id } else { $null }
        backend_port = $BackendPort
        frontend_port = $FrontendPort
        started_at = (Get-Date).ToString('s')
        backend_log = $backendErr
        frontend_log = $frontendErr
    } | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding UTF8

    Write-Host ''
    Write-Host "Started successfully: http://127.0.0.1:$FrontendPort" -ForegroundColor Green
    Write-Host "Runtime logs: $runtimeDir" -ForegroundColor DarkGray
    Write-Host 'Use the one-click stop CMD file to stop the app.' -ForegroundColor DarkGray

    if (-not $NoBrowser) {
        Start-Process "http://127.0.0.1:$FrontendPort"
    }
} catch {
    Stop-StartedProcess $frontendProcess
    Stop-StartedProcess $backendProcess
    Write-Host ''
    Write-Host "Startup failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Log folder: $runtimeDir" -ForegroundColor Yellow
    exit 1
}
