"""打包相关的一致性检查。

打包态的问题有个共同特点：**开发态永远测不出来**，只有在目标机器上双击
exe 才暴露。所以这里把「最容易漏、且漏了必崩」的几处对齐关系用测试钉住，
不必等构建完再发现。

（真正的端到端验证是打包产物的 ``--selftest``，见 ``docs/用户手册.md``。）
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC = REPO_ROOT / "usbswitch.spec"
ICON = REPO_ROOT / "src" / "usbswitch" / "resources" / "app.ico"
LINUX_BRIDGE = REPO_ROOT / "scripts" / "linux_bridge.py"

ICO_HEADER = struct.Struct("<HHH")
ICO_ENTRY = struct.Struct("<BBBBHHII")


# --------------------------------------------------------------------------- #
# 图标
# --------------------------------------------------------------------------- #


def test_exe_icon_exists():
    """exe 图标是构建期嵌进 PE 资源段的，不能像界面图标那样运行时画。"""
    assert ICON.exists(), (
        f"缺少 {ICON}\n请先执行：python scripts/make_icon.py"
    )
    assert ICON.stat().st_size > 1000, "图标文件过小，可能是空文件"


def test_exe_icon_is_valid_and_multi_size():
    """Windows 会按 DPI 与场景挑最合适的一档，只有单一尺寸会糊。"""
    blob = ICON.read_bytes()
    reserved, kind, count = ICO_HEADER.unpack_from(blob, 0)

    assert reserved == 0
    assert kind == 1, "type 必须是 1（图标）"
    assert count >= 4, f"至少要有 4 档尺寸，实际 {count}"

    sizes = set()
    for index in range(count):
        offset = ICO_HEADER.size + ICO_ENTRY.size * index
        width, height, _colors, _res, _planes, _bpp, size, data_offset = ICO_ENTRY.unpack_from(
            blob, offset
        )
        width = width or 256
        height = height or 256
        sizes.add(width)
        assert width == height, "图标必须是正方形"
        assert data_offset + size <= len(blob), "图标数据段越界"
        # ICO 允许直接内嵌 PNG（Vista 以后都支持）
        assert blob[data_offset : data_offset + 8] == b"\x89PNG\r\n\x1a\n", (
            f"{width}px 那一档不是内嵌 PNG"
        )

    assert 256 in sizes, "缺少 256px 档（大图标视图会用）"
    assert 16 in sizes, "缺少 16px 档（任务栏 / 资源管理器小图标会用）"


# --------------------------------------------------------------------------- #
# spec 与代码路径的对齐
# --------------------------------------------------------------------------- #


def test_spec_packages_linux_bridge_script():
    """①「打包后远程安装必崩」的防线。

    冻结态下 ``paths.linux_bridge_script()`` 会去
    ``_MEIPASS/usbswitch/resources/remote/`` 找这个脚本。spec 里的 datas 目标
    必须与之对齐 —— 对不上时开发态一切正常，打包后「安装远程桥接」直接失败。
    """
    text = SPEC.read_text(encoding="utf-8")

    assert "scripts" in text and "linux_bridge.py" in text, "spec 没有带上远端桥接脚本"
    assert "usbswitch/resources/remote" in text, (
        "datas 的目标路径必须与 paths.linux_bridge_script() 的冻结态路径一致"
    )
    assert LINUX_BRIDGE.exists(), "远端桥接脚本本身不见了"


def test_frozen_script_path_matches_spec_destination(monkeypatch: pytest.MonkeyPatch):
    """把两条路径真算一遍再比，避免只靠字符串匹配。"""
    from usbswitch.core import paths

    monkeypatch.setattr(paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(paths.sys, "_MEIPASS", r"C:\fake\meipass", raising=False)

    resolved = paths.linux_bridge_script()
    expected = Path(r"C:\fake\meipass") / "usbswitch" / "resources" / "remote" / "linux_bridge.py"

    assert resolved == expected


def test_spec_uses_onedir_not_onefile():
    """onefile 每次启动都要解压整个包到临时目录，冷启动明显更慢。"""
    text = SPEC.read_text(encoding="utf-8")

    assert "COLLECT(" in text, "没有 COLLECT 说明不是 onedir"
    assert "exclude_binaries=True" in text


def test_spec_is_windowed_but_keeps_traceback():
    text = SPEC.read_text(encoding="utf-8")

    assert "console=False" in text, "GUI 程序不该弹黑框"
    # 窗口态没有控制台，未捕获异常如果不写进弹窗就彻底看不见了
    assert "disable_windowed_traceback=False" in text


def test_spec_declares_bleak_winrt_backend():
    """②bleak 的后端是运行时按平台动态挑的，PyInstaller 静态分析看不见。"""
    text = SPEC.read_text(encoding="utf-8")

    assert "bleak.backends.winrt.client" in text
    assert "bleak.backends.winrt.scanner" in text
    assert "usbswitch.selftest" in text, "自检入口是运行时才 import 的，也要声明"


def _declared_hidden_imports() -> list[str]:
    """从 spec 的 HIDDEN_IMPORTS 列表里把模块名抠出来。"""
    text = SPEC.read_text(encoding="utf-8")
    start = text.index("HIDDEN_IMPORTS = [")
    end = text.index("]", start)
    block = text[start:end]

    names = []
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith('"'):
            continue
        names.append(line.split('"')[1])
    return names


def test_declared_bleak_hidden_imports_actually_exist():
    """隐藏导入不许靠猜。

    踩过：我按印象写了 ``bleak.backends.winrt.service``，构建日志里出现
    ``ERROR: Hidden import '...' not found`` —— 虽然只是噪音、不影响运行，
    但它说明这份清单没有被验证过，下次就可能猜错一个真正需要的模块。
    """
    declared = [name for name in _declared_hidden_imports() if name.startswith("bleak.")]
    assert declared, "spec 里没有声明任何 bleak 后端模块"

    import importlib

    missing = []
    for name in declared:
        try:
            importlib.import_module(name)
        except ImportError as exc:  # noqa: PERF203
            missing.append(f"{name} ({exc})")

    assert not missing, "spec 里声明了并不存在的模块：\n  " + "\n  ".join(missing)


def test_every_bleak_winrt_module_is_declared():
    """反过来也要查：包里有几个 winrt 模块，spec 就得声明几个。

    只声明一部分的话，打包后某个代码路径会在运行时才 ImportError。
    """
    import pkgutil

    import bleak.backends.winrt as winrt_backend

    actual = {
        f"bleak.backends.winrt.{mod.name}"
        for mod in pkgutil.iter_modules(winrt_backend.__path__)
    }
    declared = set(_declared_hidden_imports())

    assert actual <= declared, f"这些模块存在但 spec 没声明：{sorted(actual - declared)}"


def test_spec_version_comes_from_single_source():
    text = SPEC.read_text(encoding="utf-8")

    assert "from usbswitch.version import __version__" in text, (
        "版本号必须取自 version.py，不能在 spec 里写死一份"
    )


def test_spec_does_not_exclude_shiboken():
    """排除 shiboken6 会让 PySide6 在打包态直接崩。"""
    text = SPEC.read_text(encoding="utf-8")

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert '"shiboken6.Shiboken"' not in stripped, "不能排除 shiboken6"


def test_launcher_inserts_src_for_dev_runs():
    """launcher 要能在开发态直接跑（调试打包入口用），也要能在打包态跑。"""
    launcher = REPO_ROOT / "packaging" / "launcher.py"
    text = launcher.read_text(encoding="utf-8")

    assert 'getattr(sys, "frozen"' in text, "没有区分冻结态"
    assert "sys.path.insert" in text, "开发态需要把 src 加进 sys.path"
    assert "from usbswitch.main import _run" in text


# --------------------------------------------------------------------------- #
# 构建清单：产物自己要知道「我是什么时候构建的」
# --------------------------------------------------------------------------- #


def test_spec_writes_and_packages_build_manifest():
    """没有清单就没法回答「需要重新打包吗」—— 这是本项目的真实事故防线。

    界面改版后用户双击的仍是两小时前的包，看到的是旧界面，而程序不报错。
    """
    text = SPEC.read_text(encoding="utf-8")

    assert "_build.json" in text, "spec 没有生成构建清单"
    assert "source_stamp" in text, "清单里必须记录构建时的源码时间戳"
    assert '"usbswitch"' in text, "清单要放在 usbswitch/ 包目录下（build_info 就在旁边找）"


def test_manifest_destination_matches_build_info_lookup():
    """清单落点必须与 ``build_info.manifest_path()`` 一致。

    一边写 ``usbswitch/_build.json``、另一边找别处 —— 症状是打包后
    「构建身份」永远显示「打包」而没有时间，且新鲜度检查静默失效。
    """
    from usbswitch.core import build_info

    # 开发态下 manifest_path() 指向 core/ 的同级，即 usbswitch/_build.json
    assert build_info.manifest_path() == (
        REPO_ROOT / "src" / "usbswitch" / build_info.MANIFEST_NAME
    )


def test_manifest_name_matches_spec_literal():
    from usbswitch.core import build_info

    assert f'"{build_info.MANIFEST_NAME}"' in SPEC.read_text(encoding="utf-8") or (
        build_info.MANIFEST_NAME in SPEC.read_text(encoding="utf-8")
    )


def test_selftest_checks_build_freshness():
    """自检里必须有这一项 —— 它是唯一能自动发现「跑的是旧产物」的地方。"""
    text = (REPO_ROOT / "src" / "usbswitch" / "selftest.py").read_text(encoding="utf-8")

    assert "_check_build_identity" in text
    assert "freshness_problem" in text


def test_selftest_asserts_new_layout_parts():
    """自检不能只验「窗口能建起来」—— 改版前后都能建起来。

    必须断言新版特有的部件存在，否则「装了旧界面」会被自检放过去。
    """
    text = (REPO_ROOT / "src" / "usbswitch" / "selftest.py").read_text(encoding="utf-8")

    assert "HeroPanel" in text, "自检没有断言新版 Hero 面板存在"


def test_selftest_ui_import_check_itself_runs():
    """自检的检查项自己也要被测 —— 否则它会静默失效。

    这条是被真事逼出来的：删掉 ``switch_panel.py`` 后，``_check_ui_imports``
    里手写的模块清单没跟着改，于是自检报了一个**假失败**，而单测全绿
    （没有任何用例碰过这个函数）。
    """
    from usbswitch import selftest

    detail = selftest._check_ui_imports()

    assert "导入正常" in detail


def test_selftest_ui_import_check_enumerates_instead_of_hardcoding():
    """清单必须来自目录枚举。

    写死清单的话，删文件→假失败、加文件→漏检，两个方向都会错。
    """
    text = (REPO_ROOT / "src" / "usbswitch" / "selftest.py").read_text(encoding="utf-8")

    assert "walk_packages" in text, "UI 导入检查应当是枚举式的，不是手写清单"
