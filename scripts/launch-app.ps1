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
            $listener = Get-NetTCPConnection -State Listen -LocalAddress '127.0.0.1' -LocalPort $Port -ErrorAction SilentlyContinue
            if ($listener.OwningProcess -eq $Process.Id) { return }
            # On Windows, the venv Python executable launches the real Python as a child.
            if ($Name -eq 'backend' -and $listener -and -not $Process.HasExited) {
                $child = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
                if ($child.ParentProcessId -eq $Process.Id -and $child.CreationDate -ge $Process.StartTime) {
                    return
                }
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
    $ports = [Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners().Port
    foreach ($port in $BackendPort, $FrontendPort) {
        if ($port -in $ports) { throw "Port $port is already in use; no existing process was stopped." }
    }

    $python = Join-Path $root '.venv\Scripts\python.exe'
    $frontendDir = Join-Path $root 'frontend'
    $vite = 'node_modules/vite/bin/vite.js'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf) -or
        -not (Test-Path -LiteralPath (Join-Path $frontendDir $vite) -PathType Leaf)) {
        throw 'Missing dependencies. Run scripts\setup.ps1 first.'
    }
    $node = (Get-Command node.exe -CommandType Application).Source
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
