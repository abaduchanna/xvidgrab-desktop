# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec - XVidGrab Desktop (Tkinter build)
# Build:  python -m PyInstaller xvidgrab_desktop.spec --noconfirm --clean

a = Analysis(
    ['xvidgrab_desktop.py'],
    pathex=[],
    binaries=[],
    datas=[('icons', 'icons')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PySide6', 'pytest'],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='XVidGrab-Desktop',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon='icons/icon.ico',
)
