"""反查产物 PYZ 里 theme 模块的字节码，确认 QSS 深化真被打进去了。

用法：
    python scripts/verify_pack_theme.py "dist/USB Switch Console/USB Switch Console.exe"

判据（与 09-19 的手工数字对齐）：
    - usbswitch.ui.theme 的字符串常量里必须出现 PALETTES / build_stylesheet / state_fg
    - co_names 数量应比旧版明显多（旧版 34 → 新版 43 量级）
"""
from __future__ import annotations

import sys
from pathlib import Path

from PyInstaller.archive.readers import CArchiveReader


def collect_strings(code, out: set[str], depth: int = 0) -> None:
    if depth > 12:
        return
    for const in code.co_consts:
        if isinstance(const, str):
            out.add(const)
        elif hasattr(const, "co_consts"):
            collect_strings(const, out, depth + 1)


def main() -> int:
    exe = Path(sys.argv[1])
    pyz = CArchiveReader(str(exe)).open_embedded_archive("PYZ.pyz")
    print("PYZ 模块数:", len(pyz.toc))

    code = pyz.extract("usbswitch.ui.theme")
    strings: set[str] = set()
    collect_strings(code, strings)
    # 函数名 / 属性名在 co_names（与 co_consts 分开存），两者都要查。
    names: set[str] = set(code.co_names)

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
    print("  co_names  :", len(code.co_names))

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
    wnames: set[str] = set(widgets.co_names)
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
