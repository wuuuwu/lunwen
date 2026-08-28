#!/bin/bash

set -euo pipefail

project_root="$(cd "$(dirname "$0")/.." && pwd)"
pyinstaller="$project_root/.venv/bin/pyinstaller"
dist_root="$project_root/dist-course-macos"
work_root="$project_root/build-course-macos"
app_bundle="$dist_root/CoursePaperReviewer.app"
app_executable="$app_bundle/Contents/MacOS/CoursePaperReviewer"
archive="$dist_root/CoursePaperReviewer-macos-arm64.zip"
checksum="$archive.sha256"

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "This build must run on macOS." >&2
    exit 1
fi
if [[ "$(uname -m)" != "arm64" ]]; then
    echo "This build requires a native Apple Silicon (arm64) runner." >&2
    exit 1
fi
if [[ ! -x "$pyinstaller" ]]; then
    echo "PyInstaller executable not found: $pyinstaller" >&2
    exit 1
fi

cd "$project_root"
"$pyinstaller" \
    --noconfirm \
    --clean \
    --distpath "$dist_root" \
    --workpath "$work_root" \
    course-paper-reviewer-macos.spec

if [[ ! -x "$app_executable" ]]; then
    echo "Packaged application executable not found: $app_executable" >&2
    exit 1
fi

run_probe() {
    local argument="$1"
    local name="$2"
    shift 2
    "$@" "$app_executable" "$argument"
    echo "$name packaged self-test: passed"
}

# The Keychain probe only verifies backend discovery. It intentionally does
# not read, create, or delete a Keychain item on the unattended CI runner.
run_probe \
    "--self-test-system-credential-backend" \
    "macOS Keychain backend" \
    env
run_probe "--self-test-database" "SQLite/aiosqlite" env
run_probe "--self-test-resources" "Bundled resources" env
run_probe \
    "--self-test-report-export" \
    "Markdown/PDF report export" \
    env QT_QPA_PLATFORM=offscreen
run_probe "--self-test-batch-resources" "Course batch resources" env
run_probe "--self-test-batch-output" "Batch naming/CSV/XLSX output" env
run_probe \
    "--self-test-gui-startup" \
    "Qt GUI startup" \
    env QT_QPA_PLATFORM=offscreen

rm -f "$archive" "$checksum"
/usr/bin/ditto -c -k --sequesterRsrc --keepParent "$app_bundle" "$archive"
archive_hash="$(shasum -a 256 "$archive" | awk '{print $1}')"
printf '%s  %s\n' "$archive_hash" "$(basename "$archive")" > "$checksum"

echo "Application: $app_bundle"
echo "Archive: $archive"
echo "SHA-256: $checksum"
