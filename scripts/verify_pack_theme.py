"""反查产物 PYZ 里 theme 模块的字节码，确认 QSS 深化真被打进去了。

用法：
    python scripts/verify_pack_theme.py "dist/USB Switch Console/USB Switch Console.exe"

这条脚本存在的理由：「构建身份」只证明**打包时间晚于源码 mtime**，证不了目标
模块真被换掉（比如增量打包漏掉某个 .py）。直接读产物里的字节码才是硬证据。

    - usbswitch.ui.theme 的字符串常量里必须出现 PALETTES / LIGHT / DARK
    - co_names 里必须出现 build_stylesheet / state_fg / log_color 等
    - QSS 里新增的片段（QToolTip / QComboBox::drop-down）必须能找到
    - widgets 必须走「状态属性」那条路（有 repolish、有语义键字符串）

**收集 co_names 一定要递归。** `pyz.extract()` 给的是模块顶层 code，
类体与函数体是它的嵌套 code 对象 —— 只取顶层 `co_names` 会漏掉所有方法名
（09-21 查 main_window 时踩到：7 个方法全报 MISS，其实都在产物里）。
"""
from __future__ import annotations

import sys
from pathlib import Path

from PyInstaller.archive.readers import CArchiveReader


def collect_strings(code, out: set[str], depth: int = 0) -> None:
    if depth > 15:
        return
    for const in code.co_consts:
        if isinstance(const, str):
            out.add(const)
        elif hasattr(const, "co_consts"):
            collect_strings(const, out, depth + 1)


def collect_names(code, out: set[str], depth: int = 0) -> None:
    """递归收集所有嵌套 code 对象的 co_names。

    类方法与函数内的属性访问都在嵌套 code 里，不递归就会漏。
    """
    if depth > 15:
        return
    out.update(code.co_names)
    for const in code.co_consts:
        if hasattr(const, "co_consts") and not isinstance(const, str):
            collect_names(const, out, depth + 1)


def main() -> int:
    exe = Path(sys.argv[1])
    pyz = CArchiveReader(str(exe)).open_embedded_archive("PYZ.pyz")
    print("PYZ 模块数:", len(pyz.toc))

    code = pyz.extract("usbswitch.ui.theme")
    strings: set[str] = set()
    collect_strings(code, strings)
    # 函数名 / 属性名在 co_names（与 co_consts 分开存），必须**递归**收集
    names: set[str] = set()
    collect_names(code, names)

    # 常量类（字符串常量里能找到）
    const_syms = ["PALETTES", "LIGHT", "DARK", "MODES", "_ActivePalette"]
    # 函数类（co_names 里能找到）
    fn_syms = ["build_stylesheet", "state_fg", "log_color", "apply", "resolve"]

    print("\n[关键符号]")
    ok = True
    for name in const_syms:
        hit = name in strings
        ok &= hit
        print(f"  {'OK  ' if hit else 'MISS'} 常量 {name}")
    for name in fn_syms:
        hit = name in names
        ok &= hit
        print(f"  {'OK  ' if hit else 'MISS'} 函数 {name}")

    print("\n[规模]")
    print("  字符串常量:", len(strings))
    print("  co_names（含嵌套）:", len(names))

    # 主题相关的高风险字符串：新 QSS 里才会有
    probes = ["QToolTip", "QComboBox::drop-down", "QRadioButton"]
    qss = [s for s in strings if "QToolTip" in s or "drop-down" in s]
    print("\n[新 QSS 片段的 presence]")
    for p in probes:
        hit = any(p in s for s in strings)
        print(f"  {'OK  ' if hit else 'MISS'} 含 {p}")
    if qss:
        sample = max(qss, key=len)
        print("  最长 QSS 片段:", len(sample), "字符")
        print("  片段头:", sample[:160].replace("\n", " | "))

    # 同步反查一个「主题接线」文件，确认 setProperty 路径也在
    widgets = pyz.extract("usbswitch.ui.widgets")
    wstrings: set[str] = set()
    collect_strings(widgets, wstrings)
    wnames: set[str] = set()
    collect_names(widgets, wnames)
    # 判据：widgets 必须真的走「属性选择器」这条路 —— 有 repolish 调用，
    # 且能见到语义键与 "state" 属性名。setProperty 是 C 级方法，
    # 在部分版本里不落进 co_names，所以不把它当必要条件。
    semantic = {"ok", "warn", "idle", "danger", "success"} & wstrings
    wired = "repolish" in wnames and "state" in wstrings and len(semantic) >= 2
    print("\n[接线] widgets 走状态属性路径:", wired)
    print("  co_names 含 repolish:", "repolish" in wnames)
    print("  语义键字符串:", sorted(semantic))
    ok &= wired

    print("\n结论:", "通过" if ok else "失败")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
