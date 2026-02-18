# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
from PyInstaller.utils.hooks import collect_dynamic_libs
import sys

# 项目根目录
project_root = Path('.').resolve()

# 收集 pyzbar 依赖的动态库
pyzbar_dlls = collect_dynamic_libs('pyzbar')

block_cipher = None

a = Analysis(
    ['../credential.py'],               # 主脚本
    pathex=[str(project_root)],
    binaries=pyzbar_dlls,            # 添加 pyzbar 的 DLL
    datas=[],                         # 如果有额外文件可加
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='credential',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,  # True 表示显示控制台，扫码用
)
