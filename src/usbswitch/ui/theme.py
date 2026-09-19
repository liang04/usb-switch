"""界面主题 —— 设计令牌与 QSS。

三个概念
--------

**令牌先行。** 字号、间距、圆角都从 :data:`FONT` / :data:`SPACE` / :data:`RADIUS`
取，不在各处写裸数字。之前的版本只有 13px 一档字号、间距 8/12/14/16 混用，
没有层级可言。

**状态要能画在卡片上。** 每个语义色给三档（前景 / 浅底 / 描边），
否则「当前挂载的 Host」和「空闲的 Host」在界面上一模一样。

**着色靠 QSS 属性选择器，不靠 setStyleSheet。** 后者把颜色写死在单个控件上，
换主题时得改十几处，而且会盖掉控件级样式。控件只负责
``setProperty("state", "ok")`` + 重新 polish，颜色由这里统一定义。
"""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# 颜色
# --------------------------------------------------------------------------- #

THEME: dict[str, str] = {
    "bg": "#f4f5f7",
    "surface": "#ffffff",
    "card": "#ffffff",  # 兼容旧调用点，与 surface 同义
    "surface_alt": "#f7f8fa",
    "border": "#e4e7ea",
    "border_strong": "#c2c7cf",
    "text": "#1f2328",
    "muted": "#6b7280",
    "text_faint": "#9aa1a9",
    "accent": "#2f6fed",
    "accent_hover": "#2a63d4",
    "accent_bg": "#eaf1fe",
    "accent_border": "#2f6fed",
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
}

#: 语义状态 -> 前景色。控件拿它给文字/圆点着色。
STATE_FG: dict[str, str] = {
    "ok": THEME["success"],
    "warn": THEME["warning"],
    "error": THEME["danger"],
    "info": THEME["accent"],
    "idle": THEME["idle"],
}

#: 语义状态 -> 中性色（用于「无数据」「未启用」这类不该被强调的值）
STATE_NEUTRAL = "idle"

# --------------------------------------------------------------------------- #
# 令牌
# --------------------------------------------------------------------------- #

FONT: dict[str, int] = {
    "hero": 20,     # U 盘连接标题
    "title": 15,    # 分区标题、卡片标题
    "body": 13,     # 正文与控件
    "caption": 12,  # 次要说明、状态栏
    "micro": 11,    # 徽标
}

SPACE: dict[str, int] = {"xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 24}

RADIUS: dict[str, int] = {"card": 12, "control": 6, "pill": 99}

#: 日志级别 -> 颜色
LOG_COLORS: dict[str, str] = {
    "debug": THEME["text_faint"],
    "info": "#4b5563",
    "success": THEME["success"],
    "warning": THEME["warning"],
    "error": THEME["danger"],
}


# --------------------------------------------------------------------------- #
# QSS
# --------------------------------------------------------------------------- #

STYLESHEET = f"""
QWidget {{
    font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
    font-size: {FONT['body']}px;
    color: {THEME['text']};
}}
QMainWindow, QWidget#Root {{ background: {THEME['bg']}; }}

/* ---------------------------------------------------------------- 顶栏 -- */

QFrame#TopBar {{
    background: {THEME['surface']};
    border-bottom: 1px solid {THEME['border']};
}}
QLabel#TopBarName {{ font-size: {FONT['title']}px; font-weight: 500; }}
QLabel#TopBarDetail {{ font-size: {FONT['caption']}px; color: {THEME['muted']}; }}

/* ---------------------------------------------------------- U 盘连接 -- */

QLabel#HeroTitle {{ font-size: {FONT['hero']}px; font-weight: 500; }}
QLabel#HeroSubtitle {{ font-size: {FONT['caption']}px; color: {THEME['muted']}; }}

QFrame#HeroCard {{
    background: {THEME['surface']};
    border: 1px solid {THEME['border_strong']};
    border-radius: {RADIUS['card']}px;
}}
QFrame#HeroCard:hover {{ border-color: {THEME['muted']}; }}
QFrame#HeroCard[state="current"] {{
    background: {THEME['accent_bg']};
    border: 2px solid {THEME['accent_border']};
}}
QFrame#HeroCard[state="failed"] {{
    background: {THEME['danger_bg']};
    border: 2px solid {THEME['danger_border']};
}}
QFrame#HeroCard[variant="off"] {{
    background: transparent;
    border: 1px dashed {THEME['border_strong']};
}}
QFrame#HeroCard[variant="off"]:hover {{ border-color: {THEME['muted']}; }}
QFrame#HeroCard:disabled {{ background: {THEME['bg']}; border-color: {THEME['border']}; }}

QLabel#CardTitle {{ font-size: {FONT['title']}px; font-weight: 500; }}
QFrame#HeroCard[state="current"] QLabel#CardTitle {{ color: #14418f; }}
QLabel#CardLine {{ font-size: {FONT['caption']}px; color: {THEME['muted']}; }}
QFrame#HeroCard[state="current"] QLabel#CardLine {{ color: #21518f; }}

/* ------------------------------------------------------------ 折叠分区 -- */

QWidget#Section {{
    background: {THEME['surface']};
    border-bottom: 1px solid {THEME['border']};
}}
QWidget#SectionHeader {{ background: transparent; }}
QWidget#SectionHeader:hover {{ background: {THEME['surface_alt']}; }}
QLabel#SectionTitle {{ font-size: {FONT['title']}px; font-weight: 500; }}
QWidget#Section[alert="true"] QLabel#SectionTitle {{ color: {THEME['danger']}; }}
QLabel#SectionSummary {{ font-size: {FONT['caption']}px; color: {THEME['muted']}; }}

/* --------------------------------------------------------------- 徽标 -- */

QLabel#Chip {{
    font-size: {FONT['micro']}px;
    padding: 1px 8px;
    border-radius: 9px;
    background: {THEME['idle_bg']};
    color: {THEME['muted']};
}}
QLabel#Chip[state="ok"] {{ background: {THEME['success_bg']}; color: {THEME['success']}; }}
QLabel#Chip[state="warn"] {{ background: {THEME['warning_bg']}; color: {THEME['warning']}; }}
QLabel#Chip[state="error"] {{ background: {THEME['danger_bg']}; color: {THEME['danger']}; }}
QLabel#Chip[state="info"] {{ background: {THEME['accent_bg']}; color: {THEME['accent']}; }}

/* ------------------------------------------------------------- 状态栏 -- */

QWidget#StatusBar {{
    background: {THEME['surface_alt']};
    border-top: 1px solid {THEME['border']};
}}
QLabel#StatusText {{ font-size: {FONT['caption']}px; color: {THEME['muted']}; }}
QLabel#StatusBuild {{ font-size: {FONT['caption']}px; color: {THEME['idle']}; }}
QWidget#StatusDivider {{ background: {THEME['border']}; }}

/* ------------------------------------------------------------- 通用件 -- */

QLabel#Muted {{ color: {THEME['muted']}; font-size: {FONT['caption']}px; }}
QLabel#Value {{ font-weight: 500; }}
QLabel#Warn {{ color: {THEME['warning']}; font-size: {FONT['caption']}px; }}

QGroupBox {{
    background: {THEME['surface']};
    border: 1px solid {THEME['border']};
    border-radius: {RADIUS['card']}px;
    margin-top: 14px;
    padding: 14px 14px 12px 14px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: {THEME['muted']};
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

QPushButton {{
    background: {THEME['surface']};
    border: 1px solid {THEME['border_strong']};
    border-radius: {RADIUS['control']}px;
    padding: 6px 13px;
}}
QPushButton:hover {{ background: #f0f2f5; }}
QPushButton:pressed {{ background: #e6e9ee; }}
QPushButton:disabled {{ color: {THEME['idle']}; border-color: {THEME['border']}; background: {THEME['surface_alt']}; }}

QPushButton#Primary {{
    background: {THEME['accent']};
    border-color: {THEME['accent']};
    color: #ffffff;
}}
QPushButton#Primary:hover {{ background: {THEME['accent_hover']}; }}
QPushButton#Primary:disabled {{ background: #b9cbf3; border-color: #b9cbf3; color: #ffffff; }}

QPushButton#Danger {{ color: {THEME['danger']}; border-color: {THEME['danger_border']}; }}
QPushButton#Danger:hover {{ background: {THEME['danger_bg']}; }}

QPushButton#Ghost {{
    background: transparent;
    border-color: transparent;
    color: {THEME['muted']};
    padding: 5px 10px;
}}
QPushButton#Ghost:hover {{ background: #eceff3; color: {THEME['text']}; }}

QPlainTextEdit {{
    background: {THEME['surface_alt']};
    border: 1px solid {THEME['border']};
    border-radius: {RADIUS['control']}px;
    font-family: Consolas, "Courier New", monospace;
    font-size: {FONT['caption']}px;
    padding: 6px;
}}

QLineEdit, QSpinBox, QComboBox {{
    background: {THEME['surface']};
    border: 1px solid {THEME['border_strong']};
    border-radius: {RADIUS['control']}px;
    padding: 5px 8px;
}}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border: 1px solid {THEME['accent']}; }}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{
    background: {THEME['surface_alt']};
    color: {THEME['idle']};
}}

QScrollArea {{ background: transparent; border: none; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {THEME['border_strong']}; border-radius: 5px; min-height: 24px; }}
QScrollBar::handle:vertical:hover {{ background: {THEME['muted']}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: {THEME['border_strong']}; border-radius: 5px; min-width: 24px; }}
QScrollBar::handle:horizontal:hover {{ background: {THEME['muted']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
"""
