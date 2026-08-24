# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = collect_data_files("paper_reviewer")
datas += [
    ("migrations", "migrations"),
    ("alembic.ini", "."),
    (
        "configs/rubrics/unscored_draft.yaml",
        "paper_reviewer/resources/configs",
    ),
    (
        "configs/review_profiles/three_reviewer.yaml",
        "paper_reviewer/resources/configs",
    ),
    (
        "configs/rubrics/zhejiang_undergraduate_thesis_v2.yaml",
        "paper_reviewer/resources/configs",
    ),
    (
        "configs/review_profiles/zhejiang_undergraduate_specialists_v1.yaml",
        "paper_reviewer/resources/configs",
    ),
    (
        "configs/review_profiles/zhejiang_independent_panel_v1.yaml",
        "paper_reviewer/resources/configs",
    ),
]
hiddenimports = collect_submodules("keyring.backends")
hiddenimports += collect_submodules(
    "aiosqlite",
    filter=lambda name: not name.startswith("aiosqlite.tests"),
)
# keyring selects the Windows backend dynamically, and pywin32-ctypes redirects
# its implementation modules at runtime. Keep these imports explicit so a
# portable build cannot silently fall back to an unavailable backend.
hiddenimports += [
    "sqlalchemy.dialects.sqlite.aiosqlite",
    "win32ctypes.pywin32.pywintypes",
    "win32ctypes.pywin32.win32cred",
]
hiddenimports += collect_submodules("win32ctypes.core.ctypes")
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
    name="PaperReviewer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="PaperReviewer",
)
