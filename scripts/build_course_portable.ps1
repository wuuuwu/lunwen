$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PyInstaller = Join-Path $ProjectRoot ".venv\Scripts\pyinstaller.exe"
$DistRoot = Join-Path $ProjectRoot "dist-course"
$WorkRoot = Join-Path $ProjectRoot "build-course"

if (-not (Test-Path -LiteralPath $PyInstaller)) {
    throw "PyInstaller executable not found: $PyInstaller"
}

Push-Location $ProjectRoot
try {
    & $PyInstaller --noconfirm --clean --distpath $DistRoot --workpath $WorkRoot course-paper-reviewer.spec
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }
    $PortableDirectory = Join-Path $DistRoot "CoursePaperReviewer"
    $PortableExecutable = Join-Path $PortableDirectory "CoursePaperReviewer.exe"
    foreach ($Probe in @(
        @{ Argument = "--self-test-credentials"; Name = "Credential Manager" },
        @{ Argument = "--self-test-database"; Name = "SQLite/aiosqlite" },
        @{ Argument = "--self-test-resources"; Name = "Bundled resources" },
        @{ Argument = "--self-test-report-export"; Name = "Markdown/PDF report export" },
        @{ Argument = "--self-test-batch-resources"; Name = "Course batch resources" },
        @{ Argument = "--self-test-batch-output"; Name = "Batch naming/CSV/XLSX output" },
        @{ Argument = "--self-test-gui-startup"; Name = "Qt GUI startup" }
    )) {
        $ProbeProcess = Start-Process -FilePath $PortableExecutable `
            -ArgumentList $Probe.Argument -Wait -PassThru -WindowStyle Hidden
        if ($ProbeProcess.ExitCode -ne 0) {
            throw "$($Probe.Name) packaged self-test failed with exit code $($ProbeProcess.ExitCode)"
        }
        Write-Host "$($Probe.Name) packaged self-test: passed"
    }
    $Archive = Join-Path $DistRoot "CoursePaperReviewer-portable.zip"
    Compress-Archive -Path "$PortableDirectory\*" -DestinationPath $Archive -Force
    $ChecksumPath = "$Archive.sha256"
    $ArchiveHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Archive).Hash
    Set-Content -LiteralPath $ChecksumPath `
        -Value "$ArchiveHash  $([System.IO.Path]::GetFileName($Archive))" `
        -Encoding ascii
    Write-Host "Portable build: $PortableExecutable"
    Write-Host "Archive: $Archive"
    Write-Host "SHA-256: $ChecksumPath"
}
finally {
    Pop-Location
}
