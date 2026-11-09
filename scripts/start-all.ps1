<#
  Idempotent launcher for the Star Ledger app.
  ASCII-only source is intentional for Windows PowerShell 5 compatibility.
#>
param(
    [int]$BackendPort = 8000,
    [int]$FrontendPort = 5173
)

$ErrorActionPreference = 'Stop'
$root = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$scriptsDir = $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$backendScript = Join-Path $scriptsDir 'start-backend.ps1'
$frontendScript = Join-Path $scriptsDir 'start-frontend.ps1'
$powershellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
if (-not (Test-Path -LiteralPath $powershellExe)) {
    $command = Get-Command powershell.exe -ErrorAction SilentlyContinue
    if ($null -ne $command) { $powershellExe = $command.Source }
}

function Get-ListenerPids {
    param([int]$Port)

    $listenerIds = @()
    try {
        $listenerIds = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
            Select-Object -ExpandProperty OwningProcess -Unique |
            ForEach-Object { [int]$_ })
    } catch {
        # Use netstat below when Get-NetTCPConnection is unavailable or denied.
    }

    if ($listenerIds.Count -eq 0) {
        $netstatLines = & netstat.exe -ano -p tcp 2>$null
        foreach ($netstatLine in $netstatLines) {
            if ([string]$netstatLine -match "^\s*TCP\s+\S+:$Port\s+\S+\s+LISTENING\s+(\d+)\s*$") {
                $listenerIds += [int]$Matches[1]
            }
        }
    }

    return @($listenerIds | Select-Object -Unique)
}

function Test-ExpectedBackend {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$BackendPort/health" -TimeoutSec 3
        $live = Invoke-RestMethod -Uri "http://127.0.0.1:$BackendPort/api/ledger/live" -TimeoutSec 8

        # Do not compare database paths returned as Unicode JSON strings here.
        # Windows PowerShell 5.1 may decode application/json without a charset using
        # the system ANSI code page, corrupting Chinese characters in a valid path.
        # Instead verify health, live-ledger invariants, and the listening process:
        # it must be this project's own .venv Python running backend.main:app.
        if ([string]$health.status -ne 'ok') { return $false }
        if ($null -eq $live.invariants_ok -or -not [bool]$live.invariants_ok) { return $false }
        if ($null -eq $live.count -or [int]$live.count -lt 0) { return $false }
        if ($null -eq $health.transactions -or [int]$health.transactions -lt [int]$live.count) { return $false }

        $listenerIds = @(Get-ListenerPids -Port $BackendPort)
        if ($listenerIds.Count -eq 0) { return $false }
        foreach ($listenerId in $listenerIds) {
            if ($null -ne (Find-ProjectUvicornRoot -ListenerPid ([int]$listenerId))) {
                return $true
            }
        }
        return $false
    } catch {
        return $false
    }
}

function Test-ProjectFrontend {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$FrontendPort/" -TimeoutSec 3
        if ($response.StatusCode -lt 200 -or $response.StatusCode -ge 300) { return $false }

        # Check only ASCII HTML markers. Comparing the Chinese <title> can fail in
        # Windows PowerShell 5.1 due to response decoding, even when Vite is healthy.
        $body = [string]$response.Content
        return (($body -match 'id="app"') -and ($body -match '/src/main\.ts'))
    } catch {
        return $false
    }
}

function Find-ProjectUvicornRoot {
    param([int]$ListenerPid)

    $allProcesses = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    $processMap = @{}
    foreach ($processInfo in $allProcesses) {
        $processMap[[int]$processInfo.ProcessId] = $processInfo
    }

    $currentProcess = $processMap[[int]$ListenerPid]
    $portPattern = '(?i)--port(?:=|\s+)' + [regex]::Escape([string]$BackendPort) + '(\s|$)'

    for ($depth = 0; $depth -lt 12 -and $null -ne $currentProcess; $depth++) {
        $executablePath = [string]$currentProcess.ExecutablePath
        $commandLine = [string]$currentProcess.CommandLine
        $samePython = (-not [string]::IsNullOrWhiteSpace($executablePath)) -and ($executablePath -ieq $python)
        $isThisBackend = (($commandLine -match '(?i)uvicorn') -and ($commandLine -match '(?i)backend\.main:app') -and ($commandLine -match $portPattern))

        if ($samePython -and $isThisBackend) { return $currentProcess }

        $parentId = [int]$currentProcess.ParentProcessId
        if ($parentId -le 0 -or -not $processMap.ContainsKey($parentId)) { break }
        $currentProcess = $processMap[$parentId]
    }

    return $null
}

function Wait-ExpectedBackend {
    param([int]$Seconds = 30)
    for ($waitIndex = 0; $waitIndex -lt $Seconds; $waitIndex++) {
        if (Test-ExpectedBackend) { return $true }
        Start-Sleep -Seconds 1
    }
    return $false
}

function Start-ChildPowerShell {
    param(
        [string]$ScriptPath,
        [int]$Port
    )

    $argumentLine = '-NoExit -NoProfile -ExecutionPolicy Bypass -File "{0}" -Port {1}' -f $ScriptPath, $Port
    Start-Process -FilePath $powershellExe -ArgumentList $argumentLine -WorkingDirectory $root | Out-Null
}

if (-not (Test-Path -LiteralPath $python)) {
    Write-Host 'Missing .venv. Run .\scripts\setup.ps1 first.' -ForegroundColor Red
    exit 1
}
if (-not (Test-Path -LiteralPath (Join-Path $root 'frontend\node_modules'))) {
    Write-Host 'Missing frontend dependencies. Run .\scripts\setup.ps1 first.' -ForegroundColor Red
    exit 1
}
if (-not (Test-Path -LiteralPath $backendScript) -or -not (Test-Path -LiteralPath $frontendScript)) {
    Write-Host 'Missing start-backend.ps1 or start-frontend.ps1 in the scripts folder.' -ForegroundColor Red
    exit 1
}
if (-not (Test-Path -LiteralPath $powershellExe)) {
    Write-Host 'Could not find Windows PowerShell (powershell.exe).' -ForegroundColor Red
    exit 1
}

Write-Host ''
Write-Host 'Checking Star Ledger services...' -ForegroundColor Cyan

# Backend: reuse only when both the live API and database path match this project.
if (Test-ExpectedBackend) {
    Write-Host "[OK] Reusing this project's backend on port $BackendPort." -ForegroundColor Green
} else {
    $listenerIds = @(Get-ListenerPids -Port $BackendPort)
    if ($listenerIds.Count -gt 0) {
        $knownRoots = @()
        $unknownIds = @()

        foreach ($listenerId in $listenerIds) {
            $serverRoot = Find-ProjectUvicornRoot -ListenerPid ([int]$listenerId)
            if ($null -ne $serverRoot) {
                $knownRoots += $serverRoot
            } else {
                $unknownIds += [int]$listenerId
            }
        }

        if ($unknownIds.Count -gt 0) {
            Write-Host "ERROR: Port $BackendPort is used by an unverified process. Nothing was stopped." -ForegroundColor Red
            foreach ($unknownId in ($unknownIds | Select-Object -Unique)) {
                $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $unknownId" -ErrorAction SilentlyContinue
                if ($null -ne $processInfo) {
                    Write-Host ("PID {0} | {1} | {2}" -f $processInfo.ProcessId, $processInfo.Name, $processInfo.CommandLine) -ForegroundColor Yellow
                }
            }
            exit 1
        }

        $uniqueRoots = @($knownRoots | Group-Object -Property ProcessId | ForEach-Object { $_.Group[0] })
        foreach ($serverRoot in $uniqueRoots) {
            Write-Host ("Stopping this project's stale backend (PID {0})..." -f $serverRoot.ProcessId) -ForegroundColor Yellow
            & taskkill.exe /PID ([int]$serverRoot.ProcessId) /T /F 2>$null | Out-Null
        }

        $released = $false
        for ($waitIndex = 0; $waitIndex -lt 10; $waitIndex++) {
            if (@(Get-ListenerPids -Port $BackendPort).Count -eq 0) {
                $released = $true
                break
            }
            Start-Sleep -Seconds 1
        }
        if (-not $released) {
            Write-Host "ERROR: Port $BackendPort is still occupied. Check the process and retry." -ForegroundColor Red
            exit 1
        }
    }

    Write-Host 'Starting backend in a new window...' -ForegroundColor Cyan
    Start-ChildPowerShell -ScriptPath $backendScript -Port $BackendPort
    Write-Host 'Waiting for backend and live ledger API...' -ForegroundColor DarkGray
    if (-not (Wait-ExpectedBackend -Seconds 30)) {
        Write-Host 'ERROR: Backend did not become ready in 30 seconds, or its database path did not match.' -ForegroundColor Red
        Write-Host "Check http://127.0.0.1:$BackendPort/docs and the backend window." -ForegroundColor Yellow
        exit 1
    }
    Write-Host '[OK] Backend and live ledger API are ready.' -ForegroundColor Green
}

# Frontend: reuse this project when detected; never terminate an unknown listener.
if (Test-ProjectFrontend) {
    Write-Host "[OK] Reusing this project's frontend on port $FrontendPort." -ForegroundColor Green
} else {
    $frontendListenerIds = @(Get-ListenerPids -Port $FrontendPort)
    if ($frontendListenerIds.Count -gt 0) {
        Write-Host "ERROR: Port $FrontendPort is occupied by another or unverified process. Nothing was stopped." -ForegroundColor Red
        foreach ($frontendId in ($frontendListenerIds | Select-Object -Unique)) {
            $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $frontendId" -ErrorAction SilentlyContinue
            if ($null -ne $processInfo) {
                Write-Host ("PID {0} | {1} | {2}" -f $processInfo.ProcessId, $processInfo.Name, $processInfo.CommandLine) -ForegroundColor Yellow
            }
        }
        exit 1
    }

    Write-Host 'Starting frontend in a new window...' -ForegroundColor Cyan
    Start-ChildPowerShell -ScriptPath $frontendScript -Port $FrontendPort

    $frontendReady = $false
    for ($waitIndex = 0; $waitIndex -lt 30; $waitIndex++) {
        if (Test-ProjectFrontend) {
            $frontendReady = $true
            break
        }
        Start-Sleep -Seconds 1
    }
    if (-not $frontendReady) {
        Write-Host 'ERROR: Frontend did not become ready in 30 seconds. Check the frontend window.' -ForegroundColor Red
        exit 1
    }
    Write-Host '[OK] Frontend is ready.' -ForegroundColor Green
}

$starUrl = "http://127.0.0.1:$FrontendPort/star"
Write-Host ''
Write-Host 'Star Ledger is ready:' -ForegroundColor Green
Write-Host "Star map:      $starUrl" -ForegroundColor Green
Write-Host "Bill report:   http://127.0.0.1:$FrontendPort/bill" -ForegroundColor Green
Write-Host "API docs:      http://127.0.0.1:$BackendPort/docs" -ForegroundColor Green
Write-Host 'Opening browser...' -ForegroundColor Cyan
try {
    Start-Process -FilePath $starUrl
} catch {
    Write-Host 'Could not open the browser automatically. Open the Star map URL above.' -ForegroundColor Yellow
}
Write-Host 'Next time, double-click start-star.cmd again.' -ForegroundColor DarkGray
Write-Host ''
