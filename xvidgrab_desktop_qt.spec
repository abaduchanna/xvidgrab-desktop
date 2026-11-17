# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec - XVidGrab Desktop (PySide6 / Qt build)
# Build:  python -m PyInstaller xvidgrab_desktop_qt.spec --noconfirm --clean

a = Analysis(
    ['xvidgrab_desktop_qt.py'],
    pathex=[],
    binaries=[],
    datas=[('icons', 'icons')],
    # QtWebEngine = the embedded browser for search pages / in-app X login
    hiddenimports=[
        'PySide6.QtWebEngineWidgets',
        'PySide6.QtWebEngineCore',
        'PySide6.QtWebChannel',
        'PySide6.QtPrintSupport',
        'PySide6.QtNetwork',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'pytest'],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='XVidGrab-Desktop-Qt',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon='icons/icon.ico',
)
