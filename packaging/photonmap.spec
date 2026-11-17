# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 配置：单文件、无控制台的 PhotonMap.exe。

用法（仓库根目录）：.venv/Scripts/python.exe -m PyInstaller packaging/photonmap.spec
"""

from pathlib import Path

ROOT = Path(SPECPATH).parent

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT)],
    datas=[(str(ROOT / "photonmap" / "templates"), "photonmap/templates")],
    hiddenimports=["piexif", "pillow_heif"],
    excludes=["anthropic", "pytest", "_pytest"],
)

# libx265 **不能删**：pillow_heif 的 _pillow_heif.pyd 在导入时就要加载它，
# 缺了它连 HEIC/LIVP 解码都起不来（灯箱大图直接 500）。
# PIL._avif 是 AVIF 支持，扫描器根本不收 .avif，可以安全剔除。
_DROP_BINARIES = ("_avif",)
a.binaries = [
    entry for entry in a.binaries
    if not any(part in entry[0].lower() for part in _DROP_BINARIES)
]

# 文档不参与运行，别打进包里
a.datas = [entry for entry in a.datas if not entry[0].lower().endswith(".md")]

pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="PhotonMap",
    console=False,
    upx=False,
)
