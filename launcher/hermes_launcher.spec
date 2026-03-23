# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the Logos desktop launcher.

Build:
    pyinstaller launcher/hermes_launcher.spec

Output: dist/Logos/Logos.exe  (Windows)  or  dist/Logos/Logos  (macOS/Linux)

Requirements:
    pip install pyinstaller pystray Pillow
"""

import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent  # repo root

block_cipher = None

a = Analysis(
    [str(ROOT / "launcher" / "hermes_launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        # Soul files
        (str(ROOT / "souls"), "souls"),
        # Default config template
        (str(ROOT / "hermes_cli" / "config.py"), "hermes_cli"),
    ],
    hiddenimports=[
        # Gateway + agent
        "gateway.run",
        "gateway.http_api",
        "agents.hermes.agent",
        "core.model_tools",
        # Tray
        "pystray",
        "PIL",
        "PIL.Image",
        "PIL.ImageDraw",
        # Async
        "aiohttp",
        "aiohttp.web",
        # Misc runtime deps
        "yaml",
        "dotenv",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Exclude k8s — not needed in local/desktop mode
        "kubernetes",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Logos",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,           # No console window on Windows
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "launcher" / "logos.ico") if (ROOT / "launcher" / "logos.ico").exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="Logos",
)
