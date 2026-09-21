"""自检（冒烟测试）—— 开发态与打包态共用同一个入口。

为什么需要它
------------

打包产物是个**窗口态 exe**（`console=False`），没有 stdout、没有控制台，
不能靠 `print` 或 pytest 验证。而「双击能不能起来」恰恰是最容易出问题的地方：
PyInstaller 只在运行时才发现缺模块、资源路径不对、动态导入的后端没打进去。

所以专门做一个 `--selftest` 入口：

* 它走的是**与正式启动相同**的代码路径（同一套 `paths` / `config` / `bridge_server`）；
* 报告能写 JSON 文件，从而在无控制台的 exe 里也能取到结果；
* 退出码可直接被 CI / 脚本判断。

用法::

    python -m usbswitch --selftest                     # 核心检查
    python -m usbswitch --selftest --gui               # 额外构建一次主窗口（离屏）
    python -m usbswitch --selftest --json out.json     # 报告写文件
    python -m usbswitch --selftest --autostart         # 额外真写一次注册表（会还原）

打包产物同理：``USB Switch Console.exe --selftest --json out.json``
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

#: 自检里的网络操作一律短超时，避免「自检卡住」本身成为问题
PROBE_TIMEOUT = 3.0
TUNNEL_WAIT = 20.0
GUI_PUMP = 1.0
SCAN_TIMEOUT = 6.0


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    #: 关键项失败会让整体退出码非零；非关键项只记录
    critical: bool = True
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Report:
    version: str = ""
    frozen: bool = False
    executable: str = ""
    argv: list[str] = field(default_factory=list)
    platform: str = ""
    started: str = ""
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks if c.critical)

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    @property
    def failed_critical(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and c.critical]


def _emit(text: str) -> None:
    """窗口态 exe 里 ``sys.stdout`` 可能是 None，写不出去也不能抛。"""
    stream = sys.stdout
    if stream is None:
        return
    try:
        stream.write(text + "\n")
        stream.flush()
    except (OSError, ValueError, AttributeError):
        pass


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class _Runner:
    """把每个检查的异常都收敛成一条失败记录，而不是让自检中途中断。"""

    def __init__(self, report: Report) -> None:
        self.report = report

    def check(self, name: str, fn: Callable[[], str], *, critical: bool = True) -> Check:
        began = time.perf_counter()
        try:
            detail = fn() or ""
            result = Check(name=name, ok=True, detail=detail, critical=critical)
        except Exception as exc:  # noqa: BLE001 —— 自检必须跑完全部项目
            result = Check(
                name=name,
                ok=False,
                detail=f"{type(exc).__name__}: {exc}",
                critical=critical,
                extra={"traceback": traceback.format_exc(limit=6)},
            )
        elapsed = (time.perf_counter() - began) * 1000
        result.extra.setdefault("ms", round(elapsed, 1))
        self.report.checks.append(result)

        mark = "OK  " if result.ok else "FAIL"
        _emit(f"  [{mark}] {name:<28} {result.detail}")
        return result


# --------------------------------------------------------------------------- #
# 各项检查
# --------------------------------------------------------------------------- #


def _check_runtime() -> str:
    return (
        f"python {sys.version.split()[0]}，"
        f"{'打包态' if getattr(sys, 'frozen', False) else '开发态'}"
    )


def _check_build_identity() -> str:
    """这一份产物是什么时候、从哪一版源码构建的。

    这是**唯一**能自动回答「需要重新打包吗」的地方。没有它的时候，验证跑全绿
    也可能验的是旧 exe —— 「改了源码但双击的是旧构建」不会报任何错。
    仅在能定位到源码树时判定新鲜度；分发到别人机器上会如实跳过。
    """
    from usbswitch.core import build_info

    problem = build_info.freshness_problem()
    if problem:
        raise ValueError(problem)
    return build_info.current().describe()


def _check_paths() -> str:
    from usbswitch.core import paths

    data = paths.data_dir()
    data.mkdir(parents=True, exist_ok=True)
    probe = data / ".selftest-write-probe"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()
    return f"数据目录可写：{data}"


def _check_linux_bridge_script() -> str:
    """远端安装要把这个脚本 SFTP 上去，打包时最容易漏掉它。"""
    from usbswitch.core.paths import linux_bridge_script

    script = linux_bridge_script()
    if not script.exists():
        raise FileNotFoundError(f"找不到远端桥接脚本：{script}")
    size = script.stat().st_size
    if size < 500:
        raise ValueError(f"脚本过小（{size} 字节），可能是打包时被截断")
    return f"{script}（{size} 字节）"


def _check_config() -> str:
    from usbswitch.core import config as config_module

    cfg = config_module.load()
    remote = cfg.remote_bridge()
    remote_host = cfg.remote_host()
    if remote_host is None:
        # 「禁用」与「未配置」在自检里也要分得开，否则用户看到一句
        # 「未配置远程主机」会去反复重填一个本来填对了的地址
        reason = "已禁用" if not remote.enabled else "未配置"
        return f"配置读取正常（远程桥接{reason}）"

    remote = cfg.endpoint_for(remote_host).remote
    return (
        f"配置读取正常；远程角色 {remote_host.label} → {remote.ssh_target}，"
        f"桥接 {remote.bridge_port}，隧道={'开' if remote.use_tunnel else '关'}，"
        f"凭据={'已存' if remote.password else '无'}"
    )


def _check_crypto() -> str:
    from usbswitch.core import crypto

    token = crypto.protect("selftest-probe")
    if not crypto.is_encrypted(token):
        raise ValueError("加密结果未被识别为密文")
    if crypto.unprotect(token) != "selftest-probe":
        raise ValueError("加解密往返不一致")
    return f"DPAPI 往返正常（{token[:12]}…）"


def _module_version(module, dist_name: str) -> str:
    """尽力取版本号。

    版本只是**参考信息**，不该成为自检失败的理由，所以一路降级：
    ``__version__`` 属性 → ``importlib.metadata`` → ``"?"``。

    （bleak 3.x 就没有 ``__version__``；而冻结态里 ``importlib.metadata``
    也可能因为没打包 dist-info 而查不到。）
    """
    direct = getattr(module, "__version__", None)
    if isinstance(direct, str) and direct:
        return direct
    try:
        from importlib.metadata import version

        return version(dist_name)
    except Exception:  # noqa: BLE001 —— 查不到就算了
        return "?"


def _check_deps() -> str:
    import bleak
    import paramiko
    import PySide6

    parts = [
        f"PySide6 {_module_version(PySide6, 'PySide6-Essentials')}",
        f"bleak {_module_version(bleak, 'bleak')}",
        f"paramiko {_module_version(paramiko, 'paramiko')}",
    ]
    # bleak 的 WinRT 后端是**动态导入**的，PyInstaller 最容易漏
    from bleak.backends.winrt.client import BleakClientWinRT  # noqa: F401

    from bleak.backends.winrt.scanner import BleakScannerWinRT  # noqa: F401

    parts.append("winrt 后端已打包")
    return "，".join(parts)


def _check_ui_imports() -> str:
    """把 UI 与 workers 两层里的**每一个**模块都 import 一遍。

    能提前发现两类问题：少打了某个 Qt 子模块（打包态才崩），以及**改了文件名
    但忘了改引用**。

    刻意不写死模块清单 —— 清单一旦漏项就退化成「假装检查过了」，而它要防的
    恰恰是这种不同步。这条真的踩过：删掉 ``switch_panel`` 之后自检的清单里
    还留着它，于是这一项误报失败；反过来若清单里少列了新模块，就会漏检。
    改为按目录枚举，增删文件都不需要同步维护。
    """
    import importlib
    import pkgutil

    import usbswitch.ui
    import usbswitch.workers

    scanned = 0
    for package in (usbswitch.ui, usbswitch.workers):
        for info in pkgutil.walk_packages(package.__path__, prefix=f"{package.__name__}."):
            importlib.import_module(info.name)
            scanned += 1

    if scanned == 0:  # pragma: no cover —— 枚举不到模块说明打包严重出错
        raise ValueError("UI / workers 下没有枚举到任何模块")
    return f"UI / workers 全树导入正常（{scanned} 个模块）"


def _check_bridge_server() -> str:
    """起一个真实的本机桥接服务再停掉。

    刻意**不用 8737**：开发机上这类通用端口常被代理 / VPN / 安全软件占着
    （实测本机 Clash Verge 就绑了 ``0.0.0.0:8737``），用固定端口会让自检
    在别人的机器上假失败。
    """
    from usbswitch.core import http_json
    from usbswitch.core.bridge_server import BridgeServer

    port = _free_port()
    server = BridgeServer(port)
    server.start()
    try:
        payload = http_json.get_json(f"{server.base_url}/status", timeout=PROBE_TIMEOUT)
        if not payload.get("ok"):
            raise ValueError(f"/status 返回异常：{payload}")
        drives = payload.get("drives") or []
    finally:
        server.stop()

    if server.is_running:
        raise ValueError("停止后服务仍在运行")
    return f"起停正常，本机可移动磁盘 {drives or '（无）'}"


def _check_disk() -> str:
    from usbswitch.core import disk

    drives = disk.removable_drives()
    return f"removable_drives() -> {drives or '（无）'}"


def _check_ble_scan() -> str:
    """真扫一次蓝牙，确认打包后的 bleak 能干活。

    这是**最值得在打包产物里单独验一次**的一项：bleak 的后端是运行时按平台
    动态导入的（Windows 走 WinRT），PyInstaller 的静态分析完全看不见。
    开发态它必定成功，打包后少打一个模块就必现失败 —— 正是「本地全绿、
    拷到别人机器上就崩」的典型位置。

    「能扫到设备」则取决于 ESP32 是否上电，所以这一项非关键：
    扫描本身成功（拿到结果集）就算通过。
    """
    import asyncio

    from bleak import BleakScanner

    from usbswitch.core import config as config_module

    wanted = config_module.load().ble.device_name or "USB-Switch"

    results = asyncio.run(BleakScanner.discover(timeout=SCAN_TIMEOUT, return_adv=True))

    hits = []
    for device, adv in results.values():
        if device.name == wanted:
            hits.append(f"{device.name} @ {device.address} RSSI={adv.rssi}")

    if hits:
        return "、".join(hits)
    return f"扫描正常（发现 {len(results)} 个设备），未见 {wanted}（可能未上电）"


def _check_autostart_command() -> str:
    """冻结态的自启命令必须指向 exe 自身，且**路径带引号**。

    不带引号时 Windows 会在第一个空格处截断注册表值 —— ``C:\\Program Files\\...``
    必踩，而这类问题在开发态永远看不到（开发态是 python.exe，路径通常没空格）。
    """
    from usbswitch.core import autostart

    command = autostart.launch_command()
    if not command.startswith('"'):
        raise ValueError(f"命令未以引号开头，含空格路径会被截断：{command}")
    if autostart.STARTUP_FLAG not in command:
        raise ValueError(f"命令缺少 {autostart.STARTUP_FLAG}：{command}")

    frozen = bool(getattr(sys, "frozen", False))
    if frozen and str(sys.executable) not in command:
        raise ValueError(f"打包态命令未指向 exe 自身：{command}")
    if not frozen and "-m usbswitch" not in command:
        raise ValueError(f"开发态命令应使用 -m usbswitch：{command}")

    return f"{'打包态' if frozen else '开发态'}：{command}"


def _check_autostart_registry() -> str:
    """真的往注册表写一次，回读校验，然后**把原状态还原**。

    只有显式带 ``--autostart`` 才跑：它确实会改动用户的注册表（虽然是本程序
    自己的自启项，而且一定会还原）。验收标准里「打包产物的开机自启指向 exe
    本身且路径带引号」只有走一遍真实读写才算真验过。
    """
    from usbswitch.core import autostart

    before = autostart.current_command()
    try:
        written = autostart.enable()
        actual = autostart.current_command()
        if actual != written:
            raise ValueError(f"写入后回读不一致：期望 {written!r}，实际 {actual!r}")

        autostart.disable()
        if autostart.current_command() is not None:
            raise ValueError("删除后注册表仍有残留")
    finally:
        # 无论如何都要还原成进来时的样子
        if before is None:
            autostart.disable()
        else:
            autostart.enable(command=before)

    restored = autostart.current_command()
    if restored != before:
        raise ValueError(f"未能还原原状态：期望 {before!r}，实际 {restored!r}")

    return f"写入/回读/删除闭环正常，已还原（原值：{before or '无'}）"


def _check_tunnel() -> str:
    """配置了远程 + 隧道时，真实建一次隧道并探活。

    这一项在打包态尤其有价值：它同时验证了 paramiko 的加密后端、
    socket 转发、以及远端服务可达性。
    """
    from usbswitch.core import http_json
    from usbswitch.core import config as config_module
    from usbswitch.core.ssh_tunnel import SshTunnel

    cfg = config_module.load()
    host = cfg.remote_host()
    if host is None:
        reason = "已禁用" if not cfg.remote_bridge().enabled else "未配置"
        return f"远程桥接{reason}，跳过"
    remote = cfg.endpoint_for(host).remote
    if not remote.use_tunnel:
        return "隧道已关闭，跳过"
    if not remote.host.strip():
        raise ValueError("远程主机名为空")

    tunnel = SshTunnel(remote)
    tunnel.start()
    try:
        if not tunnel.wait_ready(TUNNEL_WAIT):
            raise TimeoutError(f"隧道未在 {TUNNEL_WAIT:g} 秒内就绪：{tunnel.last_error}")
        payload = http_json.get_json(f"{tunnel.base_url}/status", timeout=PROBE_TIMEOUT * 2)
        if not payload.get("ok"):
            raise ValueError(f"经隧道的 /status 返回异常：{payload}")
        disks = payload.get("disks") or []
    finally:
        tunnel.stop()

    if tunnel.local_port:
        raise ValueError("停止后本地监听未释放")
    return f"{tunnel.remote_target} 经隧道可达，远端磁盘 {disks or '（无）'}"


def _check_gui() -> str:
    """离屏构建一次真实主窗口，并确认装的是**新版版面**。

    用**临时数据目录**，绝不碰用户真实配置 —— 自检不该有副作用。

    这里刻意不只检查「能不能建起来」：改版前后主窗口都能正常构建，
    所以那个断言在旧产物上同样通过。**必须找新版才有的部件**，
    否则「界面没变」这类问题会被自检放过去。
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtWidgets import QApplication

    from usbswitch.core.models import AppConfig
    from usbswitch.ui.main_window import MainWindow
    from usbswitch.ui.panels.hero_panel import HeroPanel
    from usbswitch.ui.remote_config_dialog import RemoteConfigDialog  # noqa: F401
    from usbswitch.ui.theme import STYLESHEET

    previous = os.environ.get("USBSWITCH_DATA_DIR")
    os.environ["USBSWITCH_DATA_DIR"] = tempfile.mkdtemp(prefix="usbswitch-selftest-")
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(STYLESHEET)

    window = None
    try:
        window = MainWindow(AppConfig())
        deadline = time.monotonic() + GUI_PUMP
        while time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.02)
        title = window.windowTitle()

        hero = window.findChild(HeroPanel)
        if hero is None:
            raise ValueError(
                "主窗口里没有 HeroPanel —— 这一份构建是改版前的旧产物，请重新打包"
            )
        if not window._status_bar.build_text():
            raise ValueError("状态栏没有构建身份标记")
        sections = [key for key in ("bridge", "remote", "options", "log") if window.section(key)]
        if len(sections) < 4:
            raise ValueError(f"折叠分区不完整：{sections}")
    finally:
        if window is not None:
            window.shutdown()
        if previous is None:
            os.environ.pop("USBSWITCH_DATA_DIR", None)
        else:
            os.environ["USBSWITCH_DATA_DIR"] = previous

    if title != "USB Switch Console":
        raise ValueError(f"窗口标题异常：{title!r}")
    return f"新版版面离屏构建/退出正常（{title}，Hero 卡片 + {len(sections)} 个折叠分区）"


def _check_ui_assets() -> str:
    """图标是运行时画的，至少要能画出来。"""
    from PySide6.QtWidgets import QApplication

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QApplication.instance() or QApplication([])

    from usbswitch.ui.icons import app_icon, dot_icon
    from usbswitch.ui.theme import THEME

    if app_icon().isNull():
        raise ValueError("程序图标绘制失败")
    for key in ("success", "idle", "warning"):
        if dot_icon(THEME[key]).isNull():
            raise ValueError(f"托盘图标（{key}）绘制失败")
    return "程序图标与托盘图标均可绘制"


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def run(*, with_gui: bool = False, with_autostart: bool = False) -> Report:
    from usbswitch import __version__

    report = Report(
        version=__version__,
        frozen=bool(getattr(sys, "frozen", False)),
        executable=sys.executable,
        argv=list(sys.argv),
        platform=sys.platform,
        started=time.strftime("%Y-%m-%d %H:%M:%S"),
    )
    runner = _Runner(report)

    _emit(f"USB Switch Console v{report.version} 自检")
    _emit(f"  可执行文件：{report.executable}")
    _emit("")

    _emit("[环境与依赖]")
    runner.check("runtime", _check_runtime)
    # 放在最前面之一：构建不对的话，后面所有检查考的都是旧代码
    runner.check("build 身份 / 是否落后于源码", _check_build_identity)
    runner.check("paths.data_dir", _check_paths)
    runner.check("paths.linux_bridge_script", _check_linux_bridge_script)
    runner.check("config.load", _check_config)
    runner.check("crypto（DPAPI）", _check_crypto)
    runner.check("deps（PySide6/bleak/paramiko）", _check_deps)
    runner.check("ui 导入全树", _check_ui_imports)

    _emit("")
    _emit("[功能]")
    runner.check("bridge_server 起停", _check_bridge_server)
    runner.check("disk.removable_drives", _check_disk)
    runner.check("autostart 命令", _check_autostart_command)
    if with_autostart:
        # 真改注册表，所以只在你明确要求时跑（且一定会还原）
        runner.check("autostart 注册表读写", _check_autostart_registry)

    if with_gui:
        _emit("")
        _emit("[界面]")
        runner.check("ui 资源可绘制", _check_ui_assets)
        runner.check("主窗口离屏构建", _check_gui)

    _emit("")
    _emit("[网络]")
    # 设备没上电不该让自检整体失败，所以扫描与隧道都标为非关键
    runner.check("ble 扫描（bleak 后端）", _check_ble_scan, critical=False)
    runner.check("ssh 隧道 → 远端桥接", _check_tunnel, critical=False)

    _emit("")
    _emit("=" * 60)
    passed = sum(1 for c in report.checks if c.ok)
    _emit(f"  通过 {passed}/{len(report.checks)}")
    for item in report.failed:
        tag = "关键" if item.critical else "非关键"
        _emit(f"  [失败·{tag}] {item.name}: {item.detail}")
    _emit(f"  结论：{'通过' if report.ok else '未通过'}")
    return report


def main(argv: list[str]) -> int:
    with_gui = "--gui" in argv
    with_autostart = "--autostart" in argv
    json_path = None
    if "--json" in argv:
        index = argv.index("--json")
        if index + 1 < len(argv):
            json_path = argv[index + 1]

    report = run(with_gui=with_gui, with_autostart=with_autostart)

    if json_path:
        payload = {
            "version": report.version,
            "frozen": report.frozen,
            "executable": report.executable,
            "platform": report.platform,
            "started": report.started,
            "ok": report.ok,
            "passed": sum(1 for c in report.checks if c.ok),
            "total": len(report.checks),
            "failed_critical": [c.name for c in report.failed_critical],
            "checks": [c.to_dict() for c in report.checks],
        }
        try:
            Path(json_path).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            _emit(f"报告已写入 {json_path}")
        except OSError as exc:
            _emit(f"报告写入失败：{exc}")

    return 0 if report.ok else 1
