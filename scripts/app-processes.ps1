$root = Split-Path -Parent $PSScriptRoot
$runtimeDir = Join-Path $root '.runtime'

function Lock-App {
    New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
    try {
        return [IO.File]::Open((Join-Path $runtimeDir 'one-click.lock'), 'OpenOrCreate', 'Write', 'None')
    } catch [IO.IOException] {
        throw 'Cannot lock the app state. Another start or stop may be in progress.'
    }
}

function Get-RecordPath([string]$Name) {
    return Join-Path $runtimeDir "one-click-$Name.json"
}

function Read-AppRecord([string]$Name) {
    $path = Get-RecordPath $Name
    if (Test-Path -LiteralPath $path) {
        $record = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
        if ([int]$record.id -le 0 -or [long]$record.started -le 0) {
            throw "Invalid ${Name} process record: $path"
        }
        return $record
    }
}

function Get-RecordedProcess($Record) {
    if ($null -eq $Record) { return }
    $process = Get-Process -Id $Record.id -ErrorAction SilentlyContinue
    if ($process -and $process.StartTime.ToFileTimeUtc() -eq [long]$Record.started) {
        return $process
    }
}

function Stop-AppProcess([string]$Name, $Record) {
    $process = Get-RecordedProcess $Record
    if ($process) {
        & "$env:SystemRoot\System32\taskkill.exe" /PID $process.Id /T /F | Out-Host
        if ($LASTEXITCODE -ne 0 -or -not $process.WaitForExit(3000)) {
            throw "Could not stop $Name (PID $($process.Id)). Its process record was kept."
        }
        Write-Host "Stopped $Name."
    }
    $path = Get-RecordPath $Name
    if (Test-Path -LiteralPath $path) {
        Remove-Item -LiteralPath $path
    }
}
