param(
    [ValidateRange(1, 65535)][int]$BackendPort = 8000,
    [ValidateRange(1, 65535)][int]$FrontendPort = 5173,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/app-processes.ps1"

function Start-AppProcess([string]$Name, [string]$Executable, [string[]]$Arguments, [string]$Directory) {
    return $job.Start($Executable, $Arguments, $Directory,
        (Join-Path $runtimeDir "$Name.log"), (Join-Path $runtimeDir "$Name-error.log"))
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
            if ($listener -and $job.Contains($listener.Id)) { return }
            throw "Port $Port is not listening for the $Name process we started."
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "$Name did not start on port $Port. See $runtimeDir\$Name-error.log."
}

$lock = $null
$job = $null
$backend = $null
$frontend = $null
$frontendUrl = "http://127.0.0.1:$FrontendPort"
try {
    $lock = Lock-App
    if ($BackendPort -eq $FrontendPort) { throw 'Backend and frontend ports must be different.' }
    foreach ($name in 'backend', 'frontend') {
        if (Get-RecordedProcess (Read-AppRecord $name)) {
            throw "$name was started by an older launcher. Run scripts\stop-app.ps1 before using this launcher."
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
    Add-Type -Path (Join-Path $PSScriptRoot 'AppProcessJob.cs')
    $job = [AppProcessJob]::new()
    $backend = Start-AppProcess 'backend' $python @('-m', 'uvicorn', 'backend.main:app', '--host', '127.0.0.1', '--port', $BackendPort) $root
    Wait-App 'backend' $BackendPort '/health' $backend

    $env:API_TARGET = "http://127.0.0.1:$BackendPort"
    $frontend = Start-AppProcess 'frontend' $node @($vite, '--host', '127.0.0.1', '--port', $FrontendPort, '--strictPort') $frontendDir
    Wait-App 'frontend' $FrontendPort '/' $frontend

    Write-Host "Started: $frontendUrl (logs: $runtimeDir)" -ForegroundColor Green
    Write-Host 'Close this window or press Ctrl+C to stop the app.'
    if (-not $NoBrowser) {
        try { Start-Process $frontendUrl } catch { Write-Warning "Open $frontendUrl in your browser." }
    }
    while (-not $backend.HasExited -and -not $frontend.HasExited) {
        Start-Sleep -Milliseconds 500
    }
    $name = if ($backend.HasExited) { 'backend' } else { 'frontend' }
    throw "$name exited. See $runtimeDir\$name-error.log."
} catch {
    Write-Host "App stopped: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
} finally {
    if ($job) { $job.Dispose() }
    if ($frontend) { $frontend.Dispose() }
    if ($backend) { $backend.Dispose() }
    if ($lock) { $lock.Dispose() }
}
