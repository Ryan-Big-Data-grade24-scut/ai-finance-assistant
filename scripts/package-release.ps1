param(
    [string]$OutputPath = ""
)

$ErrorActionPreference = "Stop"

$source = [System.IO.Path]::GetFullPath((Split-Path $PSScriptRoot -Parent))
$parent = [System.IO.Path]::GetFullPath((Split-Path $source -Parent))
if (-not $OutputPath) {
    $OutputPath = Join-Path $parent "ai-finance-assistant-V3.1-final.zip"
}
$target = [System.IO.Path]::GetFullPath($OutputPath)
$stageRoot = [System.IO.Path]::GetFullPath((Join-Path $parent ".package-staging-v31"))
$stageProject = Join-Path $stageRoot "ai-finance-assistant-main"

if (-not $stageRoot.StartsWith($parent + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Unsafe staging path: $stageRoot"
}
if (-not $target.StartsWith($parent + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Output must stay inside the project parent directory: $target"
}

$excludedDirectories = @(
    ".venv", "venv", "node_modules", ".git", "dist", "__pycache__",
    ".pytest_cache", ".runtime", "coverage", "htmlcov", ".mypy_cache",
    ".ruff_cache", ".vite", "private_input"
)
$excludedExtensions = @(".pyc", ".pyo", ".db", ".sqlite", ".sqlite3")

if (Test-Path -LiteralPath $stageRoot) {
    Remove-Item -LiteralPath $stageRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $stageProject -Force | Out-Null

try {
    $copied = 0
    Get-ChildItem -LiteralPath $source -Recurse -Force -File | ForEach-Object {
        $relative = $_.FullName.Substring($source.Length).TrimStart('\', '/')
        $parts = $relative -split '[\\/]'
        $skipDirectory = $false
        foreach ($part in $parts) {
            if ($excludedDirectories -contains $part) {
                $skipDirectory = $true
                break
            }
        }
        if ($skipDirectory) { return }
        if ($_.Name -eq ".env" -or ($_.Name -like ".env.*" -and $_.Name -ne ".env.example")) { return }
        if ($excludedExtensions -contains $_.Extension.ToLowerInvariant()) { return }
        if ($_.Name -match '\.(db|sqlite|sqlite3)-(wal|shm)$') { return }

        $destination = Join-Path $stageProject $relative
        $destinationDirectory = Split-Path $destination -Parent
        if (-not (Test-Path -LiteralPath $destinationDirectory)) {
            New-Item -ItemType Directory -Path $destinationDirectory -Force | Out-Null
        }
        Copy-Item -LiteralPath $_.FullName -Destination $destination -Force
        $copied++
    }

    $required = @(
        "backend\main.py",
        "backend\db.py",
        "backend\ledger\service.py",
        "backend\ledger\migrate.py",
        "backend\mcp\server.py",
        "backend\classification\service.py",
        "backend\importer\alipay.py",
        "frontend\src\components\StarInspector.vue",
        "frontend\package.json",
        "frontend\package-lock.json",
        "frontend\vite.config.ts",
        "frontend\tsconfig.json",
        "db\schema.sql",
        "tests\test_ledger_engine_v3.py",
        "tests\test_ledger_legs_persistence.py",
        "tests\test_ai_classification.py",
        "README.md",
        "requirements.txt",
        ".gitignore"
    )
    foreach ($relative in $required) {
        if (-not (Test-Path -LiteralPath (Join-Path $stageProject $relative))) {
            throw "Required release file is missing: $relative"
        }
    }
    $v31Acceptance = Get-ChildItem -LiteralPath (Join-Path $stageProject "docs") -Recurse -File -Filter "V3.1-*.md"
    if (-not $v31Acceptance) {
        throw "Required V3.1 acceptance document is missing under docs"
    }

    $forbidden = Get-ChildItem -LiteralPath $stageProject -Recurse -Force | Where-Object {
        $relative = $_.FullName.Substring($stageProject.Length).TrimStart('\', '/')
        $parts = $relative -split '[\\/]'
        ($parts | Where-Object { $excludedDirectories -contains $_ }) -or
        (-not $_.PSIsContainer -and (
            $_.Name -eq ".env" -or
            ($_.Name -like ".env.*" -and $_.Name -ne ".env.example") -or
            ($excludedExtensions -contains $_.Extension.ToLowerInvariant()) -or
            $_.Name -match '\.(db|sqlite|sqlite3)-(wal|shm)$'
        ))
    }
    if ($forbidden) {
        throw "Forbidden files reached staging: $($forbidden.FullName -join ', ')"
    }

    $csvOutsideSamples = Get-ChildItem -LiteralPath $stageProject -Recurse -File -Filter "*.csv" | Where-Object {
        $relative = $_.FullName.Substring($stageProject.Length).TrimStart('\', '/')
        -not $relative.StartsWith("sample_data" + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)
    }
    if ($csvOutsideSamples) {
        throw "CSV outside sample_data is not allowed: $($csvOutsideSamples.FullName -join ', ')"
    }

    $textExtensions = @(".py", ".ts", ".vue", ".js", ".json", ".md", ".txt", ".ps1", ".cmd", ".sql", ".html", ".css")
    $secretPatterns = @(
        'sk-[A-Za-z0-9_-]{16,}',
        '-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----'
    )
    $secretHits = Get-ChildItem -LiteralPath $stageProject -Recurse -File | Where-Object {
        $textExtensions -contains $_.Extension.ToLowerInvariant()
    } | Select-String -Pattern $secretPatterns
    if ($secretHits) {
        throw "Potential secret found: $($secretHits.Path -join ', ')"
    }

    if (Test-Path -LiteralPath $target) {
        Remove-Item -LiteralPath $target -Force
    }
    Compress-Archive -LiteralPath $stageProject -DestinationPath $target -CompressionLevel Optimal -Force

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($target)
    try {
        $entries = @($archive.Entries | ForEach-Object { $_.FullName.Replace('\', '/') })
        $requiredEntries = $required | ForEach-Object { "ai-finance-assistant-main/" + $_.Replace('\', '/') }
        foreach ($entry in $requiredEntries) {
            if ($entries -notcontains $entry) {
                throw "ZIP verification failed; missing: $entry"
            }
        }
        if (-not ($entries | Where-Object { $_ -match '^ai-finance-assistant-main/docs/.*/V3\.1-.*\.md$' })) {
            throw "ZIP verification failed; V3.1 acceptance document is missing"
        }
        $badEntries = $entries | Where-Object {
            $_ -match '/(\.venv|venv|node_modules|\.git|dist|__pycache__|\.pytest_cache|\.runtime|coverage|htmlcov|private_input)/' -or
            $_ -match '/\.env$' -or
            $_ -match '\.(pyc|pyo|db|sqlite|sqlite3)(-wal|-shm)?$'
        }
        if ($badEntries) {
            throw "ZIP verification found forbidden entries: $($badEntries -join ', ')"
        }
        $entryCount = $entries.Count
    }
    finally {
        $archive.Dispose()
    }

    $item = Get-Item -LiteralPath $target
    $hash = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash
    Write-Output "PACKAGE_OK"
    Write-Output "Path=$($item.FullName)"
    Write-Output "Files=$entryCount"
    Write-Output "Bytes=$($item.Length)"
    Write-Output "SHA256=$hash"
}
finally {
    if (Test-Path -LiteralPath $stageRoot) {
        $resolved = [System.IO.Path]::GetFullPath($stageRoot)
        if (-not $resolved.StartsWith($parent + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Unsafe cleanup path: $resolved"
        }
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}
