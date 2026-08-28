# ruff: noqa: F821

from PyInstaller.building.osx import BUNDLE
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = collect_data_files("paper_reviewer")
datas += [
    ("migrations", "migrations"),
    ("alembic.ini", "."),
    (
        "configs/rubrics/course_paper_v1.yaml",
        "paper_reviewer/resources/configs",
    ),
    (
        "configs/review_profiles/course_paper_reviewers_v1.yaml",
        "paper_reviewer/resources/configs",
    ),
]
hiddenimports = collect_submodules("keyring.backends")
hiddenimports += [
    "keyring.backends.macOS",
    "keyring.backends.macOS.api",
    "sqlalchemy.dialects.sqlite.aiosqlite",
]
hiddenimports += collect_submodules(
    "aiosqlite",
    filter=lambda name: not name.startswith("aiosqlite.tests"),
)
hiddenimports += collect_submodules("ddgs")
hiddenimports = sorted(set(hiddenimports))

a = Analysis(
    ["src/paper_reviewer/gui/app.py"],
    pathex=["src"],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CoursePaperReviewer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch="arm64",
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="CoursePaperReviewer",
)

app = BUNDLE(
    coll,
    name="CoursePaperReviewer.app",
    icon=None,
    bundle_identifier="com.coursepaperreviewer.app",
    version="0.1.0",
    info_plist={
        "CFBundleName": "Course Paper Reviewer",
        "CFBundleDisplayName": "Course Paper Reviewer",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
    },
)
