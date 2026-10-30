# -*- mode: python ; coding: utf-8 -*-
# 由 build_green.ps1 调用；HOG_BUILD_ROOT 指向已归集好的 assets/ 与 bin/
import os

BUILD_ROOT = os.environ.get('HOG_BUILD_ROOT') or os.getcwd()
ASSETS = os.path.join(BUILD_ROOT, 'assets')
BIN = os.path.join(BUILD_ROOT, 'bin')

a = Analysis(
    ['tesla_dashcam_processor.py'],
    pathex=[],
    binaries=[],
    datas=[(ASSETS, 'assets'), (BIN, 'bin')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['matplotlib', 'scipy', 'pandas', 'pytest', 'IPython', 'notebook'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='tesla_overlay',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(ASSETS, 'HandOfGod.ico'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='上帝之手',
)
