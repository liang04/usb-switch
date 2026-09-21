"""界面主题 —— 设计令牌与 QSS。

四个概念
--------

**令牌先行。** 字号、间距、圆角都从 :data:`FONT` / :data:`SPACE` / :data:`RADIUS`
取，不在各处写裸数字。

**颜色分两套。** :data:`PALETTES` 里是明色与暗色两套完整调色板，
:data:`THEME` 是「当前生效那一套」的**活代理** —— 之所以不做成普通 dict 的
引用，是因为 ``widgets.py`` / ``tray.py`` / ``icons.py`` 以及三个面板都在
模块级 ``from .theme import THEME``。若 ``THEME`` 是普通 dict，切换主题时
已导入的模块会一直抱着旧那份，出现「主界面变了、托盘图标没变」的半吊子状态。
活代理让「切主题」只需要换掉一个内部引用。

**状态要能画在卡片上。** 每个语义色给三档（前景 / 浅底 / 描边），
否则「当前挂载的 Host」和「空闲的 Host」在界面上一模一样。暗色下这三档
不是把亮色反转就完事：浅底要压到很暗（``#12321f`` 这种），前景反而要提亮
（``#4ade80``），否则深底上的深绿根本读不出来。

**着色靠 QSS 属性选择器，不靠 setStyleSheet。** 后者把颜色写死在单个控件上，
换主题时得改十几处，而且会盖掉控件级样式。控件只负责
``setProperty("state", "ok")`` + 重新 polish，颜色由这里统一定义。
"""

from __future__ import annotations

from typing import Iterator, Mapping

# --------------------------------------------------------------------------- #
# 调色板
# --------------------------------------------------------------------------- #

#: 明色。信息密度高的桌面工具：低饱和中性底 + 一个冷色强调。
LIGHT: dict[str, str] = {
    "bg": "#f4f5f7",
    "surface": "#ffffff",
    "card": "#ffffff",  # 兼容旧调用点，与 surface 同义
    "surface_alt": "#f7f8fa",
    "surface_sunken": "#eef0f3",  # 比 bg 再沉一档：日志区、输入框的静止底
    "border": "#e4e7ea",
    "border_strong": "#c2c7cf",
    "text": "#1f2328",
    "muted": "#6b7280",
    "text_faint": "#9aa1a9",
    "accent": "#2f6fed",
    "accent_hover": "#2a63d4",
    "accent_active": "#2455b8",
    "accent_bg": "#eaf1fe",
    "accent_border": "#2f6fed",
    "accent_text": "#14418f",  # 压在当前态卡片浅底上的深蓝正文
    "accent_text_dim": "#21518f",
    "success": "#1a7f45",
    "success_bg": "#eaf6ee",
    "success_border": "#1a7f45",
    "warning": "#b26a00",
    "warning_bg": "#fdf5e6",
    "warning_border": "#d99a2b",
    "danger": "#c0392b",
    "danger_bg": "#fdeceb",
    "danger_border": "#d9584a",
    "idle": "#b0b6be",
    "idle_bg": "#f0f2f5",
    "hover": "#f0f2f5",  # 中性控件的悬停底
    "pressed": "#e6e9ee",
    "overlay": "#eceff3",  # Ghost 按钮悬停
    "on_accent": "#ffffff",  # 实心强调底上的文字
    "accent_disabled": "#b9cbf3",
    "knob": "#ffffff",
    "shadow": "#0f172a",
    "shadow_alpha": "26",  # 十六进制 alpha，拼在 shadow 后面
    "log_info": "#4b5563",
    "log_debug": "#9aa1a9",
}

#: 暗色。不是明色的反相，而是另做一套：
#: 底用带一点蓝的深灰（纯黑会让边框和阴影全糊掉），
#: 语义色统一提亮到能在 ``#1b1f26`` 上读清的程度。
DARK: dict[str, str] = {
    "bg": "#14171c",
    "surface": "#1b1f26",
    "card": "#1b1f26",
    "surface_alt": "#21262e",
    "surface_sunken": "#12151a",
    "border": "#2b313a",
    "border_strong": "#3d444f",
    "text": "#e6e9ef",
    "muted": "#9aa4b2",
    "text_faint": "#6b7480",
    "accent": "#5b8def",
    "accent_hover": "#74a0f5",
    "accent_active": "#4a7ade",
    "accent_bg": "#1c2a44",
    "accent_border": "#5b8def",
    "accent_text": "#cddcff",
    "accent_text_dim": "#a8c1f0",
    "success": "#4ade80",
    "success_bg": "#12321f",
    "success_border": "#2f7a4b",
    "warning": "#f5b544",
    "warning_bg": "#33280f",
    "warning_border": "#8a6420",
    "danger": "#ff7b6b",
    "danger_bg": "#3a1d1a",
    "danger_border": "#a04437",
    "idle": "#5a6472",
    "idle_bg": "#252a32",
    "hover": "#262c35",
    "pressed": "#2f3641",
    "overlay": "#262c35",
    "on_accent": "#ffffff",
    "accent_disabled": "#33415c",
    "knob": "#f2f4f8",
    "shadow": "#000000",
    "shadow_alpha": "66",
    "log_info": "#b6bfcc",
    "log_debug": "#6b7480",
}

PALETTES: dict[str, dict[str, str]] = {"light": LIGHT, "dark": DARK}

#: 主题模式的取值。``auto`` 跟随系统外观，需要 Qt 6.5+ 的
#: ``QStyleHints.colorScheme()``；取不到时回落明色。
MODES: tuple[str, ...] = ("auto", "light", "dark")

_MODE_LABELS: dict[str, str] = {"auto": "跟随系统", "light": "浅色", "dark": "深色"}


class _ActivePalette(Mapping[str, str]):
    """当前生效调色板的只读代理。

    存在的唯一理由：调用方（``widgets.py`` 等）在模块级写
    ``from .theme import THEME``，绑定的是**对象**。只要 ``THEME`` 这个对象
    恒定存在、内部指向可换，主题切换就不需要去改那些文件的导入方式。
    """

    def __init__(self) -> None:
        self._name = "light"

    # -- 读写 --------------------------------------------------------------- #

    @property
    def name(self) -> str:
        return self._name

    @property
    def palette(self) -> dict[str, str]:
        return PALETTES[self._name]

    def set_name(self, name: str) -> bool:
        """切换调色板。返回是否真的变了（调用方据此决定要不要重刷样式）。"""
        if name not in PALETTES:
            name = "light"
        if name == self._name:
            return False
        self._name = name
        return True

    # -- Mapping ------------------------------------------------------------ #

    def __getitem__(self, key: str) -> str:
        return PALETTES[self._name][key]

    def __iter__(self) -> Iterator[str]:
        return iter(PALETTES[self._name])

    def __len__(self) -> int:
        return len(PALETTES[self._name])

    def __repr__(self) -> str:  # pragma: no cover —— 只为调试可读
        return f"<THEME {self._name}>"


#: 当前生效的调色板。**注意它是活代理，不是快照** —— 见 :class:`_ActivePalette`。
THEME: _ActivePalette = _ActivePalette()

# --------------------------------------------------------------------------- #
# 语义映射
# --------------------------------------------------------------------------- #


def state_fg(state: str) -> str:
    """语义状态 -> 前景色。控件拿它给文字/圆点着色。

    是**函数**而不是常量字典：常量在 ``from .theme import STATE_FG`` 的那一刻
    就把亮色值固化了，暗色模式一开，圆点还是亮的。
    """
    return THEME.get(_STATE_KEYS.get(state, "idle"), THEME["idle"])


_STATE_KEYS: dict[str, str] = {
    "ok": "success",
    "warn": "warning",
    "error": "danger",
    "info": "accent",
    "idle": "idle",
}

#: 语义状态 -> 中性色（用于「无数据」「未启用」这类不该被强调的值）
STATE_NEUTRAL = "idle"


def log_color(level: str) -> str:
    """日志级别 -> 颜色。同样是函数，理由同上。"""
    key = _LOG_KEYS.get(level, "log_info")
    return THEME[key]


_LOG_KEYS: dict[str, str] = {
    "debug": "log_debug",
    "info": "log_info",
    "success": "success",
    "warning": "warning",
    "error": "danger",
}

# 兼容旧名：留着是因为历史上有人 may 从别处 import。**不要再新用它** ——
# 它在导入时就把颜色固化下来了。
LOG_COLORS: dict[str, str] = {
    "debug": LIGHT["log_debug"],
    "info": LIGHT["log_info"],
    "success": LIGHT["success"],
    "warning": LIGHT["warning"],
    "error": LIGHT["danger"],
}

# --------------------------------------------------------------------------- #
# 令牌
# --------------------------------------------------------------------------- #

#: 字号阶梯。拉开层级是这一版的重点：旧版 body 13 / title 15 只差 2px，
#: 「分区标题」和「正文」在视觉上几乎同级，整页看上去是一块平铺的文字。
#: 现在 hero 22 → title 16 → body 13 → caption 12 → micro 11，
#: 每一档至少差 1px 且在加权上错开，层级一眼可辨。
FONT: dict[str, int] = {
    "hero": 22,     # U 盘连接标题：整页唯一的大字，必须镇得住
    "title": 16,    # 分区标题、卡片标题
    "body": 13,     # 正文与控件
    "caption": 12,  # 次要说明、状态栏
    "micro": 11,    # 徽标
}

#: 间距阶梯。8 的倍数为主，xs/md 用来做「紧邻但不同组」的区分。
SPACE: dict[str, int] = {"xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 24, "xxl": 32}

RADIUS: dict[str, int] = {
    "card": 12,     # 卡片、分区
    "control": 6,   # 按钮、输入框
    "chip": 999,    # 徽标：全圆角
    "pill": 99,     # 兼容旧名
}

#: 卡片投影：``(x, y, blur, spread)``。QSS 不支持 box-shadow，
#: 真正画出来靠 :func:`card_shadow` 给 QGraphicsDropShadowEffect 用。
#: 暗色下投影几乎看不见（深底上的暗影没有对比度），是**有意保留**的 ——
#: 暗色里层次靠 ``surface`` 与 ``bg`` 的明度差表达，投影只是补一点边缘。
CARD_SHADOW: tuple[int, int, int, int] = (0, 1, 3, 0)


# --------------------------------------------------------------------------- #
# QSS
# --------------------------------------------------------------------------- #


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    value = color.lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def card_shadow(color_key: str = "shadow") -> str:
    """投影颜色，形如 ``#0f172a26``（含 alpha）。"""
    return THEME[color_key] + THEME["shadow_alpha"]


#: 勾选标记的 SVG 模板。QSS 的 ``image:`` 只吃文件路径或资源路径，
#: **不接受 data URI**，所以必须落一个临时文件。
_CHECK_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 15 15">'
    '<path d="M3.6 7.8 L6.2 10.4 L11.4 4.9" fill="none" stroke="{color}" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>'
)


def _check_svg(color: str) -> str:
    """把勾号写成临时 SVG 文件，返回 QSS 能吃的路径（正斜杠）。"""
    import tempfile
    from pathlib import Path

    name = f"usbswitch-check-{color.lstrip('#')}.svg"
    path = Path(tempfile.gettempdir()) / "usbswitch-theme" / name
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_CHECK_SVG.format(color=color), encoding="utf-8")
    return path.as_posix()


def build_stylesheet(palette: Mapping[str, str] | None = None) -> str:
    """按给定调色板生成 QSS。

    传入 ``None`` 时用当前生效的那套。主题切换的实现就是
    「换调色板 → 重新调这个函数 → ``app.setStyleSheet()``」，
    不需要重启，也不需要第二份手写的暗色 QSS。
    """
    c = THEME.palette if palette is None else palette

    return f"""
/* 默认字号与字色。所有控件继承这里，具体控件只需覆盖差异项。 */
QWidget {{
    font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
    font-size: {FONT['body']}px;
    color: {c['text']};
}}
QMainWindow, QWidget#Root {{ background: {c['bg']}; }}

/* 工具提示与菜单也要跟着主题走 —— 否则暗色下悬停会弹出一块刺眼的白 */
QToolTip {{
    background: {c['surface']};
    color: {c['text']};
    border: 1px solid {c['border_strong']};
    padding: 4px 7px;
}}
QMenu {{
    background: {c['surface']};
    border: 1px solid {c['border']};
    padding: 4px;
}}
QMenu::item {{ padding: 5px 22px 5px 12px; border-radius: {RADIUS['control']}px; }}
QMenu::item:selected {{ background: {c['hover']}; }}
QMenu::separator {{ height: 1px; background: {c['border']}; margin: 4px 6px; }}

/* ---------------------------------------------------------------- 顶栏 -- */

QFrame#TopBar {{
    background: {c['surface']};
    border-bottom: 1px solid {c['border']};
}}
QLabel#TopBarName {{ font-size: {FONT['title']}px; font-weight: 600; }}
QLabel#TopBarDetail {{ font-size: {FONT['caption']}px; color: {c['muted']}; }}

/* ---------------------------------------------------------- U 盘连接 -- */

QLabel#HeroTitle {{
    font-size: {FONT['hero']}px;
    font-weight: 600;
    letter-spacing: -0.3px;
}}
QLabel#HeroSubtitle {{ font-size: {FONT['caption']}px; color: {c['muted']}; }}

/* 卡片。QSS 画不了投影，但能把「安静」做出来：静态边框用最浅的一档，
   悬停才提起对比 —— 三张卡片同时挂着强边框时，视觉重心会被摊平。 */
QFrame#HeroCard {{
    background: {c['surface']};
    border: 1px solid {c['border']};
    border-radius: {RADIUS['card']}px;
}}
QFrame#HeroCard:hover {{ border-color: {c['border_strong']}; background: {c['hover']}; }}

/* 当前挂载：唯一被强调的卡片。2px 强调色描边 + 浅底，
   与「空闲卡片」拉开到不可能看错。 */
QFrame#HeroCard[state="current"] {{
    background: {c['accent_bg']};
    border: 2px solid {c['accent_border']};
}}
QFrame#HeroCard[state="current"]:hover {{ border-color: {c['accent_hover']}; }}
QFrame#HeroCard[state="failed"] {{
    background: {c['danger_bg']};
    border: 2px solid {c['danger_border']};
}}
QFrame#HeroCard[state="busy"] {{
    background: {c['warning_bg']};
    border: 2px solid {c['warning_border']};
}}
/* 「断开」那张：虚线，明确表达「这里不是一个可以长驻的状态」 */
QFrame#HeroCard[variant="off"] {{
    background: transparent;
    border: 1px dashed {c['border_strong']};
}}
QFrame#HeroCard[variant="off"]:hover {{ border-color: {c['muted']}; background: {c['hover']}; }}
QFrame#HeroCard:disabled {{
    background: {c['surface_alt']};
    border-color: {c['border']};
}}
/* 禁用态下的子文字要一起褪色，否则卡片灰了、标题还亮着 */
QFrame#HeroCard:disabled QLabel#CardTitle {{ color: {c['text_faint']}; }}
QFrame#HeroCard:disabled QLabel#CardLine {{ color: {c['text_faint']}; }}

QLabel#CardTitle {{ font-size: {FONT['title']}px; font-weight: 600; }}
/* 当前态卡片是浅蓝底，正文不能再走全局深灰 —— 那样在蓝底上发灰显脏 */
QFrame#HeroCard[state="current"] QLabel#CardTitle {{ color: {c['accent_text']}; }}
QLabel#CardLine {{ font-size: {FONT['caption']}px; color: {c['muted']}; }}
QFrame#HeroCard[state="current"] QLabel#CardLine {{ color: {c['accent_text_dim']}; }}
QFrame#HeroCard[state="failed"] QLabel#CardTitle {{ color: {c['danger']}; }}
QFrame#HeroCard[state="failed"] QLabel#CardLine {{ color: {c['danger']}; }}

/* ------------------------------------------------------------ 折叠分区 -- */

QWidget#Section {{
    background: {c['surface']};
    border-bottom: 1px solid {c['border']};
}}
QWidget#SectionHeader {{ background: transparent; }}
QWidget#SectionHeader:hover {{ background: {c['hover']}; }}
QLabel#SectionTitle {{ font-size: {FONT['title']}px; font-weight: 600; }}
QWidget#Section[alert="true"] QLabel#SectionTitle {{ color: {c['danger']}; }}
QLabel#SectionSummary {{ font-size: {FONT['caption']}px; color: {c['muted']}; }}

/* --------------------------------------------------------------- 徽标 -- */

QLabel#Chip {{
    font-size: {FONT['micro']}px;
    padding: 2px 9px;
    border-radius: {RADIUS['chip']}px;
    background: {c['idle_bg']};
    color: {c['muted']};
}}
QLabel#Chip[state="ok"] {{ background: {c['success_bg']}; color: {c['success']}; }}
QLabel#Chip[state="warn"] {{ background: {c['warning_bg']}; color: {c['warning']}; }}
QLabel#Chip[state="error"] {{ background: {c['danger_bg']}; color: {c['danger']}; }}
QLabel#Chip[state="info"] {{ background: {c['accent_bg']}; color: {c['accent']}; }}
/* 必须排在状态规则**之后**：同为 (0,1,1,1) 特异度，靠位置取胜，
   否则 ::disabled 压不过上面那些 [state=...] */
QLabel#Chip:disabled {{ background: {c['idle_bg']}; color: {c['text_faint']}; }}

/* ------------------------------------------------------------- 状态栏 -- */

QWidget#StatusBar {{
    background: {c['surface_alt']};
    border-top: 1px solid {c['border']};
}}
QLabel#StatusText {{ font-size: {FONT['caption']}px; color: {c['muted']}; }}
QLabel#StatusBuild {{ font-size: {FONT['caption']}px; color: {c['text_faint']}; }}
QWidget#StatusDivider {{ background: {c['border']}; }}

/* ------------------------------------------------------------- 通用件 -- */

QLabel#Muted {{ color: {c['muted']}; font-size: {FONT['caption']}px; }}
QLabel#Value {{ font-weight: 500; }}
/* 「标签 / 值」网格里带语义的值。走属性选择器而非内联色值，
   这样切主题后不用回头去改每一格。 */
QLabel#Value[state="ok"] {{ color: {c['success']}; }}
QLabel#Value[state="warn"] {{ color: {c['warning']}; }}
QLabel#Value[state="error"] {{ color: {c['danger']}; }}
QLabel#Value[state="info"] {{ color: {c['accent']}; }}
QLabel#Warn {{ color: {c['warning']}; font-size: {FONT['caption']}px; }}

/* 分区内的提示行。默认是低对比的中性说明，带语义时提亮 ——
   走属性选择器而非内联色值，切主题不用回头改每一处调用。 */
QLabel#Hint {{ color: {c['muted']}; font-size: {FONT['caption']}px; }}
QLabel#Hint[state="ok"] {{ color: {c['success']}; }}
QLabel#Hint[state="warn"] {{ color: {c['warning']}; }}
QLabel#Hint[state="error"] {{ color: {c['danger']}; }}
QLabel#Hint[state="info"] {{ color: {c['accent']}; }}

QGroupBox {{
    background: {c['surface']};
    border: 1px solid {c['border']};
    border-radius: {RADIUS['card']}px;
    margin-top: 14px;
    padding: 14px 14px 12px 14px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: {c['muted']};
    font-size: {FONT['caption']}px;
}}
/* 放进折叠分区的内容区：分区头已经给了标题，再画一遍卡片就是重复 */
QGroupBox[bare="true"] {{
    background: transparent;
    border: none;
    border-radius: 0;
    margin-top: 0;
    padding: 0;
}}
/* 对话框里的表单分组：同一个形状，但内边距收紧一档 —— 对话框里没有分区头，
   两个分组叠起来时默认那套留白会把它撑得很高 */
QGroupBox#FormGroup {{
    margin-top: 12px;
    padding: 12px 14px 12px 14px;
}}

/* ------------------------------------------------------ 对话框：分段与提示 -- */

/* 分组内的小节分隔线 */
QWidget#Rule {{ background: {c['border']}; }}
QLabel#SubTitle {{ color: {c['muted']}; font-size: {FONT['caption']}px; }}

/* 底部提示条：中性底 + 错误时换警示色。着色走属性选择器，不写死样式。 */
QFrame#HintBar {{
    background: {c['surface_alt']};
    border: 1px solid {c['border']};
    border-radius: {RADIUS['control']}px;
}}
QFrame#HintBar[state="error"] {{
    background: {c['danger_bg']};
    border-color: {c['danger_border']};
}}
/* 特异度 (0,1,1,2) > QLabel#Muted 的 (0,1,0,1)，能压过它 */
QFrame#HintBar[state="error"] QLabel#Muted {{ color: {c['danger']}; }}

/* --------------------------------------------------------------- 按钮 -- */

QPushButton {{
    background: {c['surface']};
    border: 1px solid {c['border_strong']};
    border-radius: {RADIUS['control']}px;
    padding: 6px 13px;
}}
QPushButton:hover {{ background: {c['hover']}; }}
QPushButton:pressed {{ background: {c['pressed']}; }}
QPushButton:focus {{ border-color: {c['accent']}; }}
QPushButton:disabled {{
    color: {c['text_faint']};
    border-color: {c['border']};
    background: {c['surface_alt']};
}}

QPushButton#Primary {{
    background: {c['accent']};
    border-color: {c['accent']};
    color: {c['on_accent']};
    font-weight: 500;
}}
QPushButton#Primary:hover {{ background: {c['accent_hover']}; border-color: {c['accent_hover']}; }}
QPushButton#Primary:pressed {{ background: {c['accent_active']}; border-color: {c['accent_active']}; }}
QPushButton#Primary:disabled {{
    background: {c['accent_disabled']};
    border-color: {c['accent_disabled']};
    color: {c['on_accent']};
}}

QPushButton#Danger {{ color: {c['danger']}; border-color: {c['danger_border']}; }}
QPushButton#Danger:hover {{ background: {c['danger_bg']}; }}

QPushButton#Ghost {{
    background: transparent;
    border-color: transparent;
    color: {c['muted']};
    padding: 5px 10px;
}}
QPushButton#Ghost:hover {{ background: {c['overlay']}; color: {c['text']}; }}

/* --------------------------------------------------------------- 输入 -- */

QPlainTextEdit {{
    background: {c['surface_sunken']};
    border: 1px solid {c['border']};
    border-radius: {RADIUS['control']}px;
    font-family: Consolas, "Cascadia Mono", "Courier New", monospace;
    font-size: {FONT['caption']}px;
    padding: 6px;
    selection-background-color: {c['accent']};
    selection-color: {c['on_accent']};
}}

QLineEdit, QSpinBox, QComboBox {{
    background: {c['surface']};
    border: 1px solid {c['border_strong']};
    border-radius: {RADIUS['control']}px;
    padding: 5px 8px;
    selection-background-color: {c['accent']};
    selection-color: {c['on_accent']};
}}
QLineEdit:hover, QSpinBox:hover, QComboBox:hover {{ border-color: {c['muted']}; }}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border: 1px solid {c['accent']}; }}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{
    background: {c['surface_alt']};
    color: {c['text_faint']};
    border-color: {c['border']};
}}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{
    background: {c['surface']};
    border: 1px solid {c['border']};
    selection-background-color: {c['hover']};
    selection-color: {c['text']};
    outline: none;
}}

/* 复选框 / 单选框：Windows 原生指示器在暗色下是白底白勾，必须自绘。
   **勾选时不能只给 background** —— QSS 一旦给了 background，Qt 就不再画原生
   勾号，结果是一块纯色方块，用户完全看不出勾没勾上。所以用 image 贴一张
   自己生成的 SVG 勾，见 :func:`_check_svg`。 */
QCheckBox, QRadioButton {{ spacing: 7px; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 15px; height: 15px; }}
QCheckBox::indicator {{
    border: 1px solid {c['border_strong']};
    border-radius: 4px;
    background: {c['surface']};
}}
QCheckBox::indicator:hover {{ border-color: {c['accent']}; }}
QCheckBox::indicator:checked {{
    background: {c['accent']};
    border-color: {c['accent']};
    image: url({_check_svg(c['on_accent'])});
}}
QCheckBox::indicator:checked:hover {{ background: {c['accent_hover']}; border-color: {c['accent_hover']}; }}
QCheckBox::indicator:disabled {{
    background: {c['surface_alt']};
    border-color: {c['border']};
}}
QCheckBox::indicator:checked:disabled {{
    background: {c['accent_disabled']};
    border-color: {c['accent_disabled']};
}}
QRadioButton::indicator {{
    border: 1px solid {c['border_strong']};
    border-radius: 8px;
    background: {c['surface']};
}}
QRadioButton::indicator:checked {{
    background: {c['surface']};
    border: 5px solid {c['accent']};
}}

/* ------------------------------------------------------------- 滚动条 -- */

QScrollArea {{ background: transparent; border: none; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {c['border_strong']}; border-radius: 5px; min-height: 24px; }}
QScrollBar::handle:vertical:hover {{ background: {c['muted']}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: {c['border_strong']}; border-radius: 5px; min-width: 24px; }}
QScrollBar::handle:horizontal:hover {{ background: {c['muted']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
"""


#: 当前生效的样式表。名字保持不变 —— ``main.py`` / ``selftest.py`` /
#: ``test_ui_smoke.py`` 都按这个名字取。切主题后要重新取一次，
#: 不要把它 import 到局部变量里长驻。
STYLESHEET: str = build_stylesheet()


# --------------------------------------------------------------------------- #
# 主题切换
# --------------------------------------------------------------------------- #

#: 当前模式（``auto`` / ``light`` / ``dark``）。与 ``THEME.name`` 的区别：
#: 模式是**用户的选择**，名称是**实际生效的结果**。``auto`` 下两者会不同。
_mode: str = "light"


def mode() -> str:
    """用户选择的模式。"""
    return _mode


def mode_label(value: str) -> str:
    return _MODE_LABELS.get(value, value)


def mode_labels() -> dict[str, str]:
    """下拉框用的 ``{值: 显示名}``。"""
    return dict(_MODE_LABELS)


def resolve(mode_value: str) -> str:
    """把模式解析成实际生效的调色板名。

    ``auto`` 走 Qt 的系统外观。**只认 6.5+ 的 ``QStyleHints.colorScheme()``** ——
    早期版本只能靠「窗口底色比中灰亮还是暗」去猜，而 qApp 还没建窗口时猜不准，
    宁可明确回落明色，也不要出现「浅色系统里开了个深色窗」。
    """
    if mode_value in ("light", "dark"):
        return mode_value
    try:
        from PySide6.QtGui import QGuiApplication
    except ImportError:  # pragma: no cover —— 只有无 Qt 的纯逻辑环境会走到
        return "light"
    app = QGuiApplication.instance()
    if app is None:
        return "light"
    scheme = getattr(app.styleHints(), "colorScheme", None)
    if callable(scheme):
        from PySide6.QtCore import Qt

        if scheme() == Qt.ColorScheme.Dark:
            return "dark"
    return "light"


def apply(app, mode_value: str) -> tuple[str, str]:
    """把主题应用到 ``app``，返回 ``(模式, 生效的调色板名)``。

    做三件事，缺一不可：

    1. 换掉 :data:`THEME` 指向的调色板 —— 这会让 ``widgets.py`` 里那些
       ``QPainter`` 手工绘制的控件（开关、圆点、折叠三角、托盘图标）在下次
       ``repaint`` 时自动取到新颜色；
    2. 重新生成 QSS 并 ``setStyleSheet``；
    3. 刷新所有顶层窗口与托盘图标 —— 被手工绘制的东西不重画就还是旧的。

    返回实际生效的名称，调用方据此回填「跟随系统」时到底是明是暗。
    """
    global _mode, STYLESHEET

    _mode = mode_value if mode_value in MODES else "light"
    name = resolve(_mode)
    THEME.set_name(name)
    STYLESHEET = build_stylesheet()

    if app is not None:
        app.setStyleSheet(STYLESHEET)

    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:  # pragma: no cover —— 只有无 Qt 的纯逻辑环境会走到
        return (name, name)

    for widget in QApplication.topLevelWidgets():
        widget.update()
    return _mode, name


def theme_mode_from_config(config) -> str:
    """从配置里读主题模式。读不到就给 ``light``。

    放在 theme 里而不是 config 里，是为了让「UI 外观」这件事的所有者只有一处；
    ``core`` 层不许 import Qt，所以这里只做宽容读取，不反向依赖配置类型。
    """
    try:
        value = config.window.theme
    except AttributeError:  # pragma: no cover —— 旧配置没有这个字段
        return "light"
    return value if value in MODES else "light"
