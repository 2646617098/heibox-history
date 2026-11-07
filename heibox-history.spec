# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：单文件、无控制台、体积优先。

体积的三个主要来源是 CPython 运行时、Tcl/Tk、httpx；这部分压不下去，
所以策略是把没用的东西全部排除（rich/typer 只服务 CLI，不该进 GUI 包）。
"""

from pathlib import Path

ROOT = Path(SPECPATH).resolve()
ICON = ROOT / "packaging" / "app.ico"

# CLI 才用到的 rich/typer/click、开发期工具、非必需的重型栈
EXCLUDES = [
    "rich",
    "typer",
    "click",
    "pygments",
    "pytest",
    "respx",
    "PIL",
    "numpy",
    "pandas",
    "matplotlib",
    "scipy",
    "IPython",
    "setuptools",
    "pip",
    "pkg_resources",
    "lib2to3",
    "pydoc_data",
    "tkinter.test",
    "test",
    "unittest",
]

a = Analysis(
    [str(ROOT / "packaging" / "entry_gui.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="heibox-history",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX 能再小 20%，但会触发杀软误报，不值
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    icon=str(ICON) if ICON.exists() else None,
)
