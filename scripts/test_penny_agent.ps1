param(
    [switch]$AgentOnly,
    [switch]$SkipFrontend
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $ProjectRoot

try {
    $PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path $PythonExe)) {
        $PythonExe = "python"
    }

    if ($AgentOnly) {
        & $PythonExe -m pytest tests/test_z_penny_agent_runtime.py tests/test_penny_agent_runtime_hardening.py -q
    }
    else {
        & $PythonExe -m pytest -q
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Backend tests failed with exit code $LASTEXITCODE."
    }

    if (-not $SkipFrontend) {
        Push-Location (Join-Path $ProjectRoot "frontend")
        try {
            if (-not (Test-Path (Join-Path (Get-Location) "node_modules/.bin/vue-tsc.cmd"))) {
                npm ci
                if ($LASTEXITCODE -ne 0) {
                    throw "npm ci failed with exit code $LASTEXITCODE."
                }
            }
            npm run build
            if ($LASTEXITCODE -ne 0) {
                throw "Frontend build failed with exit code $LASTEXITCODE."
            }
        }
        finally {
            Pop-Location
        }
    }

    Write-Host "Penny Agent test workflow completed successfully." -ForegroundColor Green
}
finally {
    Pop-Location
}
