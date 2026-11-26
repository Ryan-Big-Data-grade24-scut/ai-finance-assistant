param(
    [ValidateRange(1, 65535)][int]$BackendPort = 8000,
    [ValidateRange(1, 65535)][int]$FrontendPort = 5173,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/app-processes.ps1"

function Start-AppProcess([string]$Name, [string]$Executable, [string[]]$Arguments, [string]$Directory) {
    $process = Start-Process -FilePath $Executable -ArgumentList $Arguments -WorkingDirectory $Directory `
        -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtimeDir "$Name.log") `
        -RedirectStandardError (Join-Path $runtimeDir "$Name-error.log")
    $record = @{ id = $process.Id; started = $process.StartTime.ToFileTimeUtc() }
    $started[$Name] = $record
    $record | ConvertTo-Json | Set-Content -LiteralPath (Get-RecordPath $Name) -Encoding UTF8
    return $process
}

function Get-AppListeners([int]$Port) {
    $lines = & "$env:SystemRoot\System32\netstat.exe" -ano
    if ($LASTEXITCODE -ne 0) { throw "Could not check TCP port $Port." }
    foreach ($line in $lines) {
        if ($line -match "^\s*TCP\s+(\S+):$Port\s+\S+\s+LISTENING\s+(\d+)\s*$") {
            [pscustomobject]@{ Address = $Matches[1]; Id = [int]$Matches[2] }
        }
    }
}

function Wait-App([string]$Name, [int]$Port, [string]$Path, $Process) {
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    do {
        if ($Process.HasExited) { break }
        $response = $null
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port$Path" -TimeoutSec 2
        } catch {
            # The service is not ready yet.
        }
        if ($response.StatusCode -eq 200) {
            $listener = Get-AppListeners $Port | Where-Object Address -eq '127.0.0.1' | Select-Object -First 1
            $listenerId = $listener.Id
            if ($listenerId -eq $Process.Id) { return }
            # The Windows venv interpreter starts Uvicorn in a child process.
            # Uvicorn reports that child's PID in this launch's stderr log.
            if ($Name -eq 'backend' -and $listenerId -and -not $Process.HasExited) {
                $serverPattern = "^INFO:\s+Started server process \[$listenerId\]\s*$"
                $serverLog = Join-Path $runtimeDir 'backend-error.log'
                if (Select-String -LiteralPath $serverLog -Pattern $serverPattern -Quiet) { return }
            }
            throw "Port $Port is not listening for the $Name process we started."
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "$Name did not start on port $Port. See $runtimeDir\$Name-error.log."
}

$lock = $null
$started = @{}
$frontendUrl = "http://127.0.0.1:$FrontendPort"
try {
    $lock = Lock-App
    if ($BackendPort -eq $FrontendPort) { throw 'Backend and frontend ports must be different.' }
    foreach ($name in 'backend', 'frontend') {
        if (Get-RecordedProcess (Read-AppRecord $name)) {
            throw "$name is already running from this directory. Use the one-click stop file first."
        }
    }
    foreach ($port in $BackendPort, $FrontendPort) {
        if (Get-AppListeners $port) { throw "Port $port is already in use; no existing process was stopped." }
    }

    $python = Join-Path $root '.venv\Scripts\python.exe'
    $frontendDir = Join-Path $root 'frontend'
    $vite = 'node_modules/vite/bin/vite.js'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf) -or
        -not (Test-Path -LiteralPath (Join-Path $frontendDir $vite) -PathType Leaf)) {
        throw 'Missing dependencies. Run scripts\setup.ps1 first.'
    }
    $node = Get-Command node.exe -CommandType Application | Select-Object -First 1 -ExpandProperty Source
    $backend = Start-AppProcess 'backend' $python @('-m', 'uvicorn', 'backend.main:app', '--host', '127.0.0.1', '--port', $BackendPort) $root
    Wait-App 'backend' $BackendPort '/health' $backend

    $env:API_TARGET = "http://127.0.0.1:$BackendPort"
    $frontend = Start-AppProcess 'frontend' $node @($vite, '--host', '127.0.0.1', '--port', $FrontendPort, '--strictPort') $frontendDir
    Wait-App 'frontend' $FrontendPort '/' $frontend
} catch {
    Write-Host "Startup failed: $($_.Exception.Message)" -ForegroundColor Red
    foreach ($name in 'frontend', 'backend') {
        if ($started.ContainsKey($name)) {
            try { Stop-AppProcess $name $started[$name] } catch { Write-Warning $_.Exception.Message }
        }
    }
    exit 1
} finally {
    if ($lock) { $lock.Dispose() }
}

Write-Host "Started: $frontendUrl (logs: $runtimeDir)" -ForegroundColor Green
if (-not $NoBrowser) {
    try { Start-Process $frontendUrl } catch { Write-Warning "Open $frontendUrl in your browser." }
}
