"""主题与配色的回归测试。

这一版把「一个写死的 THEME 字典」改成了「双调色板 + 活代理」，
最容易坏的地方恰好是**看不见的**：

- 两套调色板的键集合不一致 → 暗色下某个颜色 KeyError，而那条路径可能
  只在某个特定状态才走到；
- ``THEME`` 被当成快照 import 走 → 切主题后部分控件停在旧色；
- ``config`` 里的合法值清单和 ``theme.MODES`` 漂移 → 存得进、读出来被收敛掉。

「活代理」这件事必须**反向验证**：光断言「切了之后 THEME 值变了」是自证 ——
用快照实现也能让当前线程看到新值。真正的判据是「先前 import 过的引用也跟着变」。
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6", reason="未安装 PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from usbswitch.ui import theme  # noqa: E402

#: 不是色值的键：``shadow_alpha`` 是拼在 ``shadow`` 后面的十六进制 alpha。
_NON_COLOR_KEYS = {"shadow_alpha"}


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _restore_light():
    """测试之间互相别污染 —— THEME 是模块级可变状态。"""
    yield
    theme.THEME.set_name("light")


# --------------------------------------------------------------------------- #
# 调色板完整性
# --------------------------------------------------------------------------- #


def test_palettes_have_identical_key_sets():
    """两套调色板必须一一对应。

    少一个键的表现是「切到暗色时某个控件 KeyError」—— 而那个控件可能只在
    「桥接异常」「隧道失败」这类少见的路径上才画，手工点不出来。
    """
    assert set(theme.LIGHT) == set(theme.DARK), (
        "两套调色板的键不一致："
        f"{sorted(set(theme.LIGHT) ^ set(theme.DARK))}"
    )


def test_palettes_are_not_empty_and_all_values_are_colors():
    for name, palette in theme.PALETTES.items():
        assert palette, f"{name} 是空的"
        for key, value in palette.items():
            if key in _NON_COLOR_KEYS:
                continue
            assert value.startswith("#"), f"{name}.{key} 不是色值：{value!r}"


def test_shadow_alpha_is_a_byte_in_hex():
    """投影 alpha 会被拼在色值后面（``#0f172a`` + ``26``），必须是两位十六进制。

    写成 ``0x26`` 或 ``38`` 拼出来是个非法颜色 —— Qt 不报错，只是不画投影。
    """
    for name, palette in theme.PALETTES.items():
        alpha = palette["shadow_alpha"]
        assert len(alpha) == 2, f"{name}.shadow_alpha 不是两位：{alpha!r}"
        int(alpha, 16)  # 非十六进制会在这里抛 ValueError


def test_dark_is_actually_dark_and_light_is_actually_light():
    """防止「两套调色板抄成一样」这种静默失效。

    按相对亮度判：底色必须真的比文字暗（暗色）/ 亮（明色）。
    只断言「两套的 bg 不相等」是不够的 —— 改成 #ffffff / #fefefe 也能过。
    """
    def luminance(hex_color: str) -> float:
        value = hex_color.lstrip("#")
        r, g, b = (int(value[i : i + 2], 16) for i in (0, 2, 4))
        return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255

    for name in ("light", "dark"):
        theme.THEME.set_name(name)
        bg = luminance(theme.THEME["bg"])
        fg = luminance(theme.THEME["text"])
        if name == "light":
            assert bg > 0.8, f"明色底不够亮：{theme.THEME['bg']}"
            assert fg < 0.3, f"明色文字不够深：{theme.THEME['text']}"
        else:
            assert bg < 0.2, f"暗色底不够暗：{theme.THEME['bg']}"
            assert fg > 0.7, f"暗色文字不够亮：{theme.THEME['text']}"


@pytest.mark.parametrize("name", ["light", "dark"])
def test_semantic_colors_have_readable_contrast(name: str):
    """状态色必须能在它自己的浅底上读出来。

    暗色最容易翻车的地方：把明色的语义色原样搬过来 —— 深绿压在深绿底上，
    圆点看着像坏的。这里按 WCAG 的 3:1（大字/图形级）判。
    """
    theme.THEME.set_name(name)
    palette = theme.THEME.palette

    def luminance(hex_color: str) -> float:
        value = hex_color.lstrip("#")
        r, g, b = (int(value[i : i + 2], 16) for i in (0, 2, 4))
        channels = []
        for c in (r, g, b):
            srgb = c / 255
            channels.append(srgb / 12.92 if srgb <= 0.03928 else ((srgb + 0.055) / 1.055) ** 2.4)
        return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]

    def ratio(a: str, b: str) -> float:
        la, lb = luminance(a), luminance(b)
        lighter, darker = max(la, lb), min(la, lb)
        return (lighter + 0.05) / (darker + 0.05)

    for semantic in ("success", "warning", "danger", "accent"):
        fg = palette[semantic]
        bg = palette[f"{semantic}_bg"]
        assert ratio(fg, bg) >= 3.0, (
            f"{name}: {semantic} 在其浅底上对比度不足 "
            f"（{fg} on {bg} = {ratio(fg, bg):.2f}:1）"
        )


# --------------------------------------------------------------------------- #
# 活代理：切主题后**先前 import 过的引用**也要跟着变
# --------------------------------------------------------------------------- #


def test_theme_is_a_live_proxy_not_a_snapshot():
    """**反向验证**：把 THEME 先绑到一个局部名上，再切主题，它必须跟着变。

    用快照（``THEME = dict(LIGHT)``）实现的话，切主题只会换掉模块里的名字，
    而 ``widgets.py`` / ``tray.py`` 在 import 时绑定的那个对象纹丝不动 ——
    表现是「主界面换肤了、托盘图标和彩点没换」。
    """
    theme.THEME.set_name("light")
    captured = theme.THEME  # 等价于 `from .theme import THEME`

    assert captured["bg"] == theme.LIGHT["bg"]

    theme.THEME.set_name("dark")

    assert captured["bg"] == theme.DARK["bg"], (
        "THEME 是快照不是活代理 —— 先前 import 过的引用没有跟着切"
    )
    assert captured["bg"] != theme.LIGHT["bg"]


def test_state_fg_follows_the_active_palette():
    """语义色取值必须是函数，不能是 import 时就固化好的常量字典。"""
    theme.THEME.set_name("light")
    light_ok = theme.state_fg("ok")

    theme.THEME.set_name("dark")
    dark_ok = theme.state_fg("ok")

    assert light_ok == theme.LIGHT["success"]
    assert dark_ok == theme.DARK["success"]
    assert light_ok != dark_ok


def test_log_color_follows_the_active_palette():
    theme.THEME.set_name("light")
    light_err = theme.log_color("error")
    theme.THEME.set_name("dark")
    assert light_err != theme.log_color("error")
    assert theme.log_color("error") == theme.DARK["danger"]


def test_unknown_state_and_level_fall_back_instead_of_raising():
    """拼错的状态键不该让界面崩 —— 回落中性色。"""
    assert theme.state_fg("这不是一个状态") == theme.THEME["idle"]
    assert theme.state_fg(None) == theme.THEME["idle"]  # type: ignore[arg-type]
    assert theme.log_color("这不是一个级别") == theme.THEME["log_info"]


# --------------------------------------------------------------------------- #
# QSS 生成
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", ["light", "dark"])
def test_build_stylesheet_paints_every_hex(name: str):
    """生成出来的 QSS 必须真的用上了各个令牌。

    f-string 里少写一层花括号（``{c['x']}`` 写成 ``c['x']``）不会报错，
    只会把字面量塞进样式表 —— Qt 静默忽略那一行，表现是「某个控件没上色」。
    """
    sheet = theme.build_stylesheet(theme.PALETTES[name])

    for key in ("text", "bg", "surface", "border"):
        assert theme.PALETTES[name][key] in sheet, f"{name}.{key} 没有出现在 QSS 里"


def test_stylesheet_survives_the_swap_and_keeps_the_anchors():
    """test_ui_smoke 依赖的两个锚点，切主题后必须仍在。"""
    for name in ("light", "dark"):
        theme.THEME.set_name(name)
        sheet = theme.build_stylesheet()
        assert "QGroupBox" in sheet
        assert "QPushButton#Primary" in sheet


def test_theme_switch_actually_changes_the_stylesheet():
    """反向验证：切主题必须让样式表**内容**变化。

    只断言「STYLESHEET 变量存在」是自证 —— 永远返回同一份也能过。
    """
    theme.THEME.set_name("light")
    light = theme.build_stylesheet()
    theme.THEME.set_name("dark")
    dark = theme.build_stylesheet()

    assert light != dark, "两个主题生成了完全相同的 QSS"


def test_apply_rebuilds_the_module_stylesheet(app):
    """``theme.apply`` 必须同时换掉调色板**和**模块级的 STYLESHEET。

    之前踩过：只换了 THEME 忘了重生成 QSS，于是 `app.setStyleSheet` 套上的
    还是旧的那份 —— 颜色全对、控件外观全没变（或者反过来）。
    """
    from usbswitch.ui import theme as theme_module

    theme_module.apply(app, "light")
    light_sheet = theme_module.STYLESHEET
    assert theme_module.THEME.name == "light"

    mode, palette = theme_module.apply(app, "dark")

    assert mode == "dark"
    assert palette == "dark"
    assert theme_module.THEME.name == "dark"
    assert theme_module.STYLESHEET != light_sheet, "STYLESHEET 没跟着重建"
    assert app.styleSheet() == theme_module.STYLESHEET, "没套到 QApplication 上"


def test_apply_clamps_unknown_mode_to_light(app):
    """改坏的配置值不该让窗口变成没样式的裸控件。"""
    mode, palette = theme.apply(app, "这不是一个主题")
    assert mode == "light"
    assert palette == "light"


# --------------------------------------------------------------------------- #
# 与配置层的约定
# --------------------------------------------------------------------------- #


def test_modes_agree_with_config():
    """``theme.MODES`` 与 ``config.THEME_MODES`` 必须一致。

    core 不许 import Qt，所以两边各留了一份清单 —— 靠这条测试钉住，
    否则会出现「界面里能选、存进配置后加载时被收敛回浅色」。
    """
    from usbswitch.core.config import THEME_MODES

    assert set(THEME_MODES) == set(theme.MODES), (
        f"两份清单漂移了：config={sorted(THEME_MODES)} theme={sorted(theme.MODES)}"
    )


@pytest.mark.parametrize("value", ["light", "dark", "auto"])
def test_theme_mode_roundtrips_through_config(value: str, data_dir):
    from usbswitch.core import config as config_module
    from usbswitch.core.models import AppConfig

    config = AppConfig()
    config.window.theme = value
    path = data_dir / "config.json"

    config_module.save(config, path)
    assert config_module.load(path).window.theme == value


@pytest.mark.parametrize("garbage", ["", "深色", "purple", "true", "0"])
def test_invalid_theme_mode_in_config_falls_back_to_light(garbage, data_dir):
    """配置文件被手工改坏时不能让程序起不来 —— 收敛成浅色即可。"""
    import json

    from usbswitch.core import config as config_module

    path = data_dir / "config.json"
    path.write_text(
        json.dumps({"version": config_module.SCHEMA_VERSION, "window": {"theme": garbage}}),
        encoding="utf-8",
    )

    assert config_module.load(path).window.theme == "light"


@pytest.mark.parametrize("raw,expected", [("DARK", "dark"), ("Light", "light"), (" AUTO ", "auto")])
def test_theme_mode_is_case_and_whitespace_insensitive(raw: str, expected: str, data_dir):
    """大小写与首尾空白不该让一个本来正确的值被收敛掉。

    非 JSON 的路径（有人手工编辑配置文件）很容易带上 ``Light`` 或空格，
    这时静默回落浅色会让人以为设置没保存。
    """
    import json

    from usbswitch.core import config as config_module

    path = data_dir / "config.json"
    path.write_text(
        json.dumps({"version": config_module.SCHEMA_VERSION, "window": {"theme": raw}}),
        encoding="utf-8",
    )

    assert config_module.load(path).window.theme == expected


def test_non_string_theme_mode_falls_back(data_dir):
    """``null`` / 数字这类不该让 str() 变成 ``"None"`` / ``"42"`` 蒙混过去。"""
    import json

    from usbswitch.core import config as config_module

    path = data_dir / "config.json"
    path.write_text(
        json.dumps({"version": config_module.SCHEMA_VERSION, "window": {"theme": None}}),
        encoding="utf-8",
    )

    assert config_module.load(path).window.theme == "light"


def test_resolve_returns_a_real_palette_name():
    for mode in theme.MODES:
        assert theme.resolve(mode) in theme.PALETTES
