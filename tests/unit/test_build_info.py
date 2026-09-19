"""构建身份的测试。

这一层值得单独测，因为它守的是一个**不会自己暴露**的失败模式：
改了源码、跑的却是旧产物。程序不会报错，用户只会觉得「界面没变」。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from usbswitch.core import build_info


def _touch(path: Path, when: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x = 1\n", encoding="utf-8")
    os.utime(path, (when, when))


# --------------------------------------------------------------------------- #
# 开发态
# --------------------------------------------------------------------------- #


def test_dev_mode_is_not_frozen_and_has_no_build_time():
    info = build_info.current()

    assert info.frozen is False
    assert info.built_at is None
    # 开发态下「源码时间戳」是实时扫出来的，必然存在
    assert info.source_stamp is not None
    assert "开发态" in info.describe()
    assert "dev" in info.short()


def test_dev_mode_never_reports_staleness():
    """开发态跑的就是源码本身，谈「落后于源码」没有意义。"""
    assert build_info.freshness_problem() is None


def test_short_includes_version():
    assert build_info.current().short().startswith("v")


# --------------------------------------------------------------------------- #
# 源码树定位
# --------------------------------------------------------------------------- #


def test_repo_source_root_finds_the_repo_from_a_nested_dir(tmp_path):
    """从仓库内任意子目录都能往上找到 src/usbswitch。"""
    repo = tmp_path / "repo"
    (repo / "src" / "usbswitch").mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    deep = repo / "src" / "usbswitch" / "core"
    deep.mkdir(parents=True)

    assert build_info.repo_source_root(deep) == repo / "src" / "usbswitch"


def test_repo_source_root_returns_none_outside_repo(tmp_path, monkeypatch):
    """别人的机器上没有源码树，必须如实返回 None 而不是瞎猜。

    注意要先把 cwd 挪出仓库 —— ``repo_source_root`` 会把当前目录也当起点，
    这是有意的（在仓库里跑 ``--selftest`` 时不必额外指定路径）。
    """
    lonely = tmp_path / "somewhere"
    lonely.mkdir()
    monkeypatch.chdir(lonely)

    assert build_info.repo_source_root(lonely) is None


def test_repo_source_root_honours_env_override(tmp_path, monkeypatch):
    """``USBSWITCH_SOURCE`` 优先 —— 在仓库外跑产物自检时靠它指路。"""
    repo = tmp_path / "repo"
    (repo / "src" / "usbswitch").mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    monkeypatch.setenv(build_info.SOURCE_ENV, str(repo))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    assert build_info.repo_source_root(elsewhere) == repo / "src" / "usbswitch"


def test_scan_source_stamp_picks_the_newest_py(tmp_path):
    root = tmp_path / "pkg"
    old = time.time() - 10_000
    _touch(root / "a.py", old)
    _touch(root / "sub" / "b.py", old + 5000)

    assert build_info.scan_source_stamp(root) == pytest.approx(old + 5000, abs=1)


def test_scan_source_stamp_ignores_non_python(tmp_path):
    root = tmp_path / "pkg"
    _touch(root / "a.py", time.time() - 10_000)
    _touch(root / "notes.txt", time.time())

    assert build_info.scan_source_stamp(root) == pytest.approx(time.time() - 10_000, abs=2)


def test_scan_source_stamp_returns_none_for_missing_dir(tmp_path):
    assert build_info.scan_source_stamp(tmp_path / "nope") is None


# --------------------------------------------------------------------------- #
# 打包态（用假清单模拟）
# --------------------------------------------------------------------------- #


@pytest.fixture()
def fake_frozen(tmp_path, monkeypatch):
    """把「打包态」拼出来：假清单 + 假源码树。

    ``current()`` 走的是 ``sys.frozen``，清单路径由 ``manifest_path()`` 决定，
    两者都可以替换，于是不需要真的打一个包就能测。
    """
    repo = tmp_path / "repo"
    source = repo / "src" / "usbswitch"
    source.mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

    manifest = tmp_path / "_build.json"
    monkeypatch.setattr(build_info, "manifest_path", lambda: manifest)
    # 直接替换判定函数，而不是去改全局的 sys.frozen —— 后者会影响别的库
    monkeypatch.setattr(build_info, "_is_frozen", lambda: True)

    def write(*, built_at: float, source_stamp: float, version: str = "1.2.3") -> None:
        manifest.write_text(
            json.dumps(
                {"version": version, "built_at": built_at, "source_stamp": source_stamp}
            ),
            encoding="utf-8",
        )

    return {"write": write, "manifest": manifest, "source": source, "repo": repo}


def test_frozen_reads_manifest(fake_frozen):
    now = time.time()
    fake_frozen["write"](built_at=now - 3600, source_stamp=now - 3600)

    info = build_info.current()

    assert info.frozen is True
    assert info.version == "1.2.3"
    assert info.built_at == pytest.approx(now - 3600, abs=1)
    assert "打包态" in info.describe()
    assert "构建" in info.short()


def test_frozen_without_manifest_does_not_crash(fake_frozen):
    """清单缺失（旧产物、被手工删掉）时要能降级，不能因此崩掉。"""
    info = build_info.current()

    assert info.frozen is True
    assert info.built_at is None
    assert info.short().endswith("打包")


def test_frozen_fresh_build_reports_no_problem(fake_frozen):
    """源码自打包之后没动过 —— 不该报落后。"""
    stamp = time.time() - 100
    _touch(fake_frozen["source"] / "a.py", stamp)
    fake_frozen["write"](built_at=stamp + 5, source_stamp=stamp)

    assert build_info.freshness_problem(fake_frozen["repo"]) is None


def test_frozen_stale_build_is_reported(fake_frozen):
    """核心场景：打包之后又改了源码 —— 必须明确报出来。"""
    built = time.time() - 7200
    _touch(fake_frozen["source"] / "a.py", time.time())
    fake_frozen["write"](built_at=built, source_stamp=built)

    problem = build_info.freshness_problem(fake_frozen["repo"])

    assert problem is not None
    assert "落后于源码" in problem
    assert "重新打包" in problem


def test_stale_report_counts_newer_files(fake_frozen):
    built = time.time() - 7200
    fake_frozen["write"](built_at=built, source_stamp=built)
    _touch(fake_frozen["source"] / "a.py", time.time())
    _touch(fake_frozen["source"] / "b.py", time.time())
    _touch(fake_frozen["source"] / "c.py", built - 100)  # 比产物旧，不该计入

    problem = build_info.freshness_problem(fake_frozen["repo"])

    assert "2 个文件" in problem


def test_small_time_skew_is_not_a_problem(fake_frozen):
    """构建紧跟编辑，两边可能只差几毫秒 —— 别让浮点误差变成误报。"""
    stamp = time.time()
    _touch(fake_frozen["source"] / "a.py", stamp)
    fake_frozen["write"](built_at=stamp, source_stamp=stamp - 0.2)

    assert build_info.freshness_problem(fake_frozen["repo"]) is None


def test_frozen_without_source_tree_skips_the_check(fake_frozen, tmp_path, monkeypatch):
    """分发到别人机器上没有源码树 —— 必须跳过，而不是当成失败。"""
    empty = tmp_path / "no-repo-here"
    empty.mkdir()
    monkeypatch.chdir(empty)
    fake_frozen["write"](built_at=time.time() - 9999, source_stamp=time.time() - 9999)

    assert build_info.freshness_problem(empty) is None


def test_frozen_without_source_stamp_skips_the_check(fake_frozen):
    """旧产物里可能没有 source_stamp 字段 —— 无从比较，跳过。"""
    fake_frozen["manifest"].write_text(
        json.dumps({"version": "0.0.1"}), encoding="utf-8"
    )

    assert build_info.freshness_problem(fake_frozen["repo"]) is None


def test_broken_manifest_is_tolerated(fake_frozen):
    """清单写坏了只该降级成「不知道」，绝不能让程序起不来。"""
    fake_frozen["manifest"].write_text("{ 这不是 JSON", encoding="utf-8")

    info = build_info.current()

    assert info.frozen is True
    assert info.built_at is None
