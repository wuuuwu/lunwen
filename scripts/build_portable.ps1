$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PyInstaller = Join-Path $ProjectRoot ".venv\Scripts\pyinstaller.exe"

if (-not (Test-Path -LiteralPath $PyInstaller)) {
    throw "PyInstaller executable not found: $PyInstaller"
}

Push-Location $ProjectRoot
try {
    & $PyInstaller --noconfirm --clean paper-reviewer.spec
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }
    $PortableExecutable = Join-Path $ProjectRoot "dist\PaperReviewer\PaperReviewer.exe"
    foreach ($Probe in @(
        @{ Argument = "--self-test-credentials"; Name = "Credential Manager" },
        @{ Argument = "--self-test-database"; Name = "SQLite/aiosqlite" },
        @{ Argument = "--self-test-resources"; Name = "Bundled resources" },
        @{ Argument = "--self-test-report-export"; Name = "Markdown/PDF report export" }
    )) {
        $ProbeProcess = Start-Process -FilePath $PortableExecutable `
            -ArgumentList $Probe.Argument -Wait -PassThru -WindowStyle Hidden
        if ($ProbeProcess.ExitCode -ne 0) {
            throw "$($Probe.Name) packaged self-test failed with exit code $($ProbeProcess.ExitCode)"
        }
        Write-Host "$($Probe.Name) packaged self-test: passed"
    }
    Compress-Archive -Path "dist\PaperReviewer\*" -DestinationPath "dist\PaperReviewer-portable.zip" -Force
    Write-Host "Portable build: dist\PaperReviewer\PaperReviewer.exe"
    Write-Host "Archive: dist\PaperReviewer-portable.zip"
}
finally {
    Pop-Location
}
