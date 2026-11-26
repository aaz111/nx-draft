# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

产物：dist/nxdraft.exe（单文件，约 15~25 MB）

exe 只打包 nxdraft 自己的 Python 代码与前端页面；NX 仍需本机安装
（NX 是商业软件，不能随包分发，且体积有数 GB）。

打包命令：
    python -m PyInstaller build.spec --clean --noconfirm

注意 nxdraft/web/index.html 必须一起打进去（datas），否则 GUI 打开是白页。
"""

import os
import sys

block_cipher = None

# ---- 图标与版本信息 ----
# 图标是可选的：assets/nxdraft.ico 不存在时退回默认图标，不让打包失败。
# PyInstaller 执行 spec 时 cwd 是仓库根（打包命令在那儿跑），所以用相对路径找。
ICON_CANDIDATE = os.path.join(os.getcwd(), "assets", "nxdraft.ico")
ICON_PATH = ICON_CANDIDATE if os.path.isfile(ICON_CANDIDATE) else None
VERSION_CANDIDATE = os.path.join(os.getcwd(), "version_info.txt")
VERSION_FILE = VERSION_CANDIDATE if os.path.isfile(VERSION_CANDIDATE) else None
if ICON_PATH is None:
    print("build: assets/nxdraft.ico 不存在，使用默认图标")
if VERSION_FILE is None:
    print("build: version_info.txt 不存在，跳过版本信息")

# exe 运行时要让 **NX 的 Python** import 到 nxdraft：NX 是另一个进程，
# 拿不到 exe 里打包的模块，所以必须把整个包目录作为 datas 释放到 _MEIPASS，
# launcher.package_root() 会把那个目录塞进 journal 的 sys.path。
pkg_dir = os.path.join("nxdraft")
web_dir = os.path.join("nxdraft", "web")
if not os.path.isdir(pkg_dir):
    raise SystemExit("缺少 nxdraft 包目录 —— 请在仓库根目录运行本脚本")

# 只带 .py 和 web 资源，跳过 __pycache__
datas = [(web_dir, os.path.join("nxdraft", "web"))]
for name in sorted(os.listdir(pkg_dir)):
    full = os.path.join(pkg_dir, name)
    if os.path.isfile(full) and name.endswith(".py"):
        datas.append((full, "nxdraft"))

hidden = [
    "nxdraft.api",
    "nxdraft.runtime",
    "nxdraft.launcher",
    "nxdraft.gui",
    "nxdraft.queue",
    "nxdraft.cli",
]

a = Analysis(
    ["nxdraft_main.py"],
    pathex=[os.getcwd()],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 这些是开发期工具，不进 exe
        "tkinter", "unittest", "pydoc", "doctest", "test",
        "pymupdf", "PIL", "numpy", "pandas",
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
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="nxdraft",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,              # UPX 常被杀软误报，关掉
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,           # 保留控制台：NX 的日志和报错要能看到
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # 图标与版本信息：右键 exe → 属性 → 详细信息 里能看到产品名/版本
    icon=ICON_PATH,
    version=VERSION_FILE,
)
