# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

    .venv\\Scripts\\python.exe -m PyInstaller usbswitch.spec --noconfirm

产物：``dist/USB Switch Console/`` —— 用 **onedir** 而不是 onefile：
onefile 每次启动都要把整个包解压到临时目录，冷启动明显更慢，而且杀软对
自解压可执行文件更敏感。代价是产物是一个目录，不是单个文件。

三个最容易踩的点，都在下面标出来了：

1. **``scripts/linux_bridge.py`` 必须作为数据文件带进去。** 冻结态下
   ``paths.linux_bridge_script()`` 会去 ``_MEIPASS/usbswitch/resources/remote/``
   找它，漏了它「远程安装」功能会在打包后直接失败 —— 而这在开发态永远测不出来。
2. **bleak 的 WinRT 后端是动态导入的**，PyInstaller 的静态分析看不见。
3. **版本号只有一个来源**（``usbswitch/version.py``），版本资源文件在这里
   现场生成，避免仓库里多一份会过期的文件。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

# SPECPATH 由 PyInstaller 注入：spec 文件所在目录
REPO = Path(SPECPATH).resolve()  # noqa: F821
sys.path.insert(0, str(REPO / "src"))

from usbswitch.version import __version__  # noqa: E402

APP_NAME = "USB Switch Console"
VERSION_PARTS = tuple(int(p) for p in (__version__.split(".") + ["0", "0", "0"])[:4])


# --------------------------------------------------------------------------- #
# 版本资源文件（现场生成，不进仓库）
# --------------------------------------------------------------------------- #


def _write_version_file(target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={VERSION_PARTS},
    prodvers={VERSION_PARTS},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [
          StringStruct('CompanyName', 'Robustel'),
          StringStruct('FileDescription', '{APP_NAME}'),
          StringStruct('FileVersion', '{__version__}'),
          StringStruct('InternalName', 'usbswitch'),
          StringStruct('LegalCopyright', 'Copyright (C) Robustel'),
          StringStruct('OriginalFilename', '{APP_NAME}.exe'),
          StringStruct('ProductName', '{APP_NAME}'),
          StringStruct('ProductVersion', '{__version__}')
        ]
      )
    ]),
    VarFileInfo([VarStruct('Translation', [0x0409, 1200])])
  ]
)
""",
        encoding="utf-8",
    )
    return target


VERSION_FILE = _write_version_file(REPO / "build" / "version_info.txt")


# --------------------------------------------------------------------------- #
# 构建清单：让产物自己知道「我是什么时候、从哪一版源码构建的」
# --------------------------------------------------------------------------- #
#
# 不生成它的话，「改了源码但跑的是旧 exe」只能靠人工比对文件时间戳来发现。
# 这条真的踩过：界面改版后用户双击的还是两小时前的包，看到的是旧界面，
# 而程序不会报任何错。--selftest 会拿清单里的源码时间戳与当前源码树比对，
# 把这个问题直接指出来（`core/build_info.py`）。


def _write_build_manifest(target: Path) -> Path:
    stamps: list[float] = []
    for path in (REPO / "src" / "usbswitch").rglob("*.py"):
        try:
            stamps.append(path.stat().st_mtime)
        except OSError:
            continue

    payload = {
        "version": __version__,
        "built_at": time.time(),
        # 构建当时的源码最新修改时间 —— 新鲜度判定的基准
        "source_stamp": max(stamps, default=time.time()),
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"[spec] 构建清单：{payload}")
    return target


# 落点必须与 core/build_info.py 的 manifest_path() 一致：usbswitch/_build.json
BUILD_MANIFEST = _write_build_manifest(REPO / "packaging" / "_build.json")

# 图标由 scripts/make_icon.py 从 ui/icons.py 的绘制代码生成
ICON_FILE = REPO / "src" / "usbswitch" / "resources" / "app.ico"
if not ICON_FILE.exists():
    raise SystemExit(
        f"缺少图标文件 {ICON_FILE}\n"
        f"请先执行：{sys.executable} scripts/make_icon.py"
    )


# --------------------------------------------------------------------------- #
# 隐藏导入：静态分析看不见的那些
# --------------------------------------------------------------------------- #

HIDDEN_IMPORTS = [
    # ②bleak 的后端是运行时按平台挑的，Windows 上走 WinRT 这一套。
    #   清单来自实际的包内容（bleak 3.x 的 winrt 后端只有这三个模块）——
    #   `tests/unit/test_packaging.py` 会逐个 import 校验，不许靠猜。
    "bleak.backends.winrt",
    "bleak.backends.winrt.client",
    "bleak.backends.winrt.scanner",
    "bleak.backends.winrt.util",
    "winrt.windows.devices.bluetooth",
    "winrt.windows.devices.bluetooth.advertisement",
    "winrt.windows.devices.bluetooth.genericattributeprofile",
    "winrt.windows.devices.enumeration",
    "winrt.windows.devices.radios",
    "winrt.windows.foundation",
    "winrt.windows.foundation.collections",
    "winrt.windows.storage.streams",
    # 自检入口是运行时才 import 的，静态分析同样看不见
    "usbswitch.selftest",
]

# --------------------------------------------------------------------------- #
# 排除项：只留真正用得上的 Qt 模块
# --------------------------------------------------------------------------- #

EXCLUDES = [
    # PySide6-Essentials 之外 / 本项目根本没用到的 Qt 模块
    "PySide6.Qt3DAnimation", "PySide6.Qt3DCore", "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput", "PySide6.Qt3DLogic", "PySide6.Qt3DRender",
    "PySide6.QtBluetooth", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets", "PySide6.QtNfc", "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtPositioning", "PySide6.QtQml", "PySide6.QtQuick",
    "PySide6.QtQuick3D", "PySide6.QtQuickControls2", "PySide6.QtQuickWidgets",
    "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtSensors",
    "PySide6.QtSerialPort", "PySide6.QtSpatialAudio", "PySide6.QtSql",
    "PySide6.QtStateMachine", "PySide6.QtSvgWidgets", "PySide6.QtTest",
    "PySide6.QtTextToSpeech", "PySide6.QtUiTools", "PySide6.QtWebChannel",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebEngineWidgets", "PySide6.QtWebSockets", "PySide6.QtXml",
    # 注意：不要排除 shiboken6 —— PySide6 运行时要加载它，排掉会直接崩
    # 开发期才用到的
    "pytest", "ruff", "setuptools", "pip", "wheel",
    "tkinter", "unittest", "pydoc", "doctest",
    # 科学计算栈：某些库会顺带拉进来，体积很大又完全用不到
    "numpy", "matplotlib", "pandas", "scipy", "PIL",
]


# --------------------------------------------------------------------------- #
# 包元数据：让 --selftest 在冻结态也能报出依赖版本
# --------------------------------------------------------------------------- #
#
# 不打包 dist-info 的话，``importlib.metadata.version("bleak")`` 查不到，
# 自检里那一栏只能显示 "?"。几 KB 的代价换一份可用的诊断信息，划算。

from PyInstaller.utils.hooks import copy_metadata  # noqa: E402

METADATA_DATAS: list = []
for distribution in ("bleak", "paramiko", "PySide6-Essentials", "PySide6", "usbswitch"):
    try:
        METADATA_DATAS += copy_metadata(distribution)
    except Exception as exc:  # noqa: BLE001 —— 名字对不上不该让构建失败
        print(f"[spec] 跳过 {distribution} 的元数据：{exc}")


a = Analysis(  # noqa: F821
    [str(REPO / "packaging" / "launcher.py")],
    pathex=[str(REPO / "src")],
    binaries=[],
    datas=[
        # ① 远端安装要 SFTP 上去的脚本，路径必须与 paths.linux_bridge_script() 对齐
        (str(REPO / "scripts" / "linux_bridge.py"), "usbswitch/resources/remote"),
        (str(ICON_FILE), "usbswitch/resources"),
        # 构建身份必须落在 usbswitch/ 包目录下：core/build_info.py 就在旁边找它
        (str(BUILD_MANIFEST), "usbswitch"),
        *METADATA_DATAS,
    ],
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX 会显著提高被杀软误报的概率，得不偿失
    console=False,      # 窗口态；自检结果靠 --json 落文件（没有 stdout）
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON_FILE),
    version=str(VERSION_FILE),
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
