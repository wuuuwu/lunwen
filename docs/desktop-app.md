# Desktop app architecture

The desktop client uses PySide6 and Qt Widgets with the native Windows title bar. It does not spawn
or parse the CLI. Both entry points reuse the application/orchestrator layer.

## UI structure

- `MainWindow` owns the native menu bar, labeled navigation, page stack, and concise status bar.
- New review, run history, rubric, and settings are persistent destinations.
- Progress and report views are contextual run-detail views.
- Long-running async review work executes in a dedicated `QThread`; Qt signals marshal events back
  to the GUI thread.
- Item collections use Qt model/view classes rather than item widgets.

## Fluent styling

The UI has one `FluentThemeManager`. A traceable resolved semantic subset of
`@fluentui/react-theme` 9.2.1 feeds `QPalette` and one generated application QSS. Widgets express
intent with properties such as `fluentAppearance`, `fluentInvalid`, `fluentBusy`, and
`fluentSeverity`; pages do not contain hard-coded colors or local style sheets.

System, light, dark, and high-contrast-aware modes are supported. The Widget Gallery forces the
primary rest, focus, disabled, invalid, busy, and severity variants for visual review.

## Local data and secrets

`platformdirs` resolves `%LOCALAPPDATA%\PaperReviewer` for the database, run artifacts, logs, and
preferences. Provider API keys are retrieved from Windows Credential Manager using `keyring`, with
environment variables retained as a CLI-compatible fallback. Keys are passed directly to model
adapters and are never written to preferences, traces, reports, or the database.

## Report export

Completed tasks expose separate **Export Markdown** and **Export PDF** actions on the report page.
Markdown is copied byte-for-byte from the canonical `report.md`; older tasks without that file are
rebuilt deterministically from their saved rubric, audit, and evaluation/meta-review snapshots.
PDF is a local, read-only A4 rendering of the same Markdown and never calls a model or the network.
Both formats use atomic replacement, require explicit overwrite confirmation, and cannot target
any run snapshot directory.

## Packaging

`paper-reviewer.spec` creates a PyInstaller onedir build and collects Qt plugins plus package data:
Fluent tokens, QSS, SVG icons, bundled rubrics/profiles, prompts, and migrations. The PowerShell
build script produces both the directory and a portable ZIP.

The top-level `configs/rubrics` and `configs/review_profiles` files are the single source of truth
for the bundled defaults. Development resolves those files directly; wheel and PyInstaller builds
copy the same files into package resources. Editing a default therefore requires no second manual
sync step.

Release QA can verify the packaged Windows Credential Manager integration without using or
overwriting a provider key. The command below creates a randomly named temporary credential,
checks it, removes it, and returns process exit code `0` on success:

```powershell
$process = Start-Process .\dist\PaperReviewer\PaperReviewer.exe `
  -ArgumentList "--self-test-credentials" -Wait -PassThru
$process.ExitCode
```

The database driver and SQLAlchemy dialect are also dynamically imported. Verify that the packaged
application can create and query a temporary SQLite database with:

```powershell
$process = Start-Process .\dist\PaperReviewer\PaperReviewer.exe `
  -ArgumentList "--self-test-database" -Wait -PassThru
$process.ExitCode
```

The packaged `--self-test-report-export` probe renders and reopens a multi-page Chinese PDF to
verify Qt PDF support, the print stylesheet, system fonts, and PyMuPDF. `scripts/build_portable.ps1`
runs the credential, database, resource, and report-export probes before creating the release ZIP.
