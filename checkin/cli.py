"""命令行入口。

常用：
    python -m checkin init
    python -m checkin doctor
    python -m checkin plan
    python -m checkin run --simulate      # 用模板契约跑通全流程
    python -m checkin status
    python -m checkin history --limit 20
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .adapters.registry import AdapterRegistry
from .config import DEFAULTS, ConfigError, config_summary, load_config, sample_config
from .executor import Executor
from .models import RunStatus
from .notifier import Notifier
from .plan import build_daily_plan, in_checkin_window
from .storage import Storage
from .utils import RunBusy, now_local, parse_date, setup_logger
from . import paths


def _project_home() -> Path:
    return paths.data_root()


def _resolve(base: Path, value: str) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else (base / p)


def _context(args) -> Dict[str, Any]:
    home = _project_home()
    config_path = Path(args.config).expanduser().resolve() if args.config else (home / "config.json")
    cfg = load_config(config_path)
    for key in ("database", "log_file", "contracts_dir"):
        cfg[key] = str(_resolve(home, cfg.get(key, DEFAULTS[key])))
    # 打包后契约内置在 exe 里：用户未显式指定自定义目录时，指向内置资源
    if paths.is_frozen() and str(cfg.get("contracts_dir", "")).endswith("contracts") \
            and cfg.get("contracts_dir") == str(home / DEFAULTS["contracts_dir"]):
        cfg["contracts_dir"] = str(paths.contracts_dir())
    logger = setup_logger(cfg)
    storage = Storage(cfg["database"])
    # 保留天数清理：避免 runs/attempt_logs/drift_events 无限增长
    try:
        pruned = storage.prune(cfg.get("retention_days", 180))
        if pruned and any(pruned.values()):
            logger.info(f"历史清理: {pruned}")
    except Exception:
        pass
    registry = AdapterRegistry(cfg["contracts_dir"], simulate=getattr(args, "simulate", False))
    notifier = Notifier(cfg.get("notify") or {}, logger)
    executor = Executor(cfg, storage, registry, notifier, logger)
    return {
        "home": home, "config_path": config_path, "cfg": cfg, "logger": logger,
        "storage": storage, "registry": registry, "notifier": notifier,
        "executor": executor,
    }


def _display_width(text: str) -> int:
    """终端显示宽度：中日韩全角字符占 2 列。"""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def print_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    if not rows:
        print("（无数据）")
        return
    table = [[str(c) if c is not None else "" for c in row] for row in rows]
    widths = [_display_width(str(h)) for h in headers]
    for row in table:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], _display_width(cell))
    def fmt(row):
        cells = []
        for i, cell in enumerate(row):
            pad = max(0, widths[i] - _display_width(cell))
            cells.append(cell + " " * pad)
        return " | ".join(cells)
    print(fmt(list(headers)))
    print("-+-".join("-" * w for w in widths))
    for row in table:
        print(fmt(row))


STATUS_LABEL = {
    RunStatus.SUCCESS.value: "本次领取",
    RunStatus.ALREADY.value: "今日已领",
    RunStatus.FAILED.value: "失败",
    RunStatus.SKIPPED.value: "跳过",
    RunStatus.DRIFT.value: "漂移",
    RunStatus.RUNNING.value: "运行中",
    RunStatus.PENDING.value: "待执行",
}


def _label(status: str) -> str:
    return STATUS_LABEL.get(status, status)


def _short_time(value: str) -> str:
    if not value:
        return "-"
    try:
        from datetime import datetime
        return datetime.fromisoformat(value).strftime("%m-%d %H:%M")
    except Exception:
        return value[:16]


# ---------------- 命令实现 ----------------
def _do_init(target: Path, *, quiet: bool = False) -> bool:
    """生成配置模板与数据目录。返回 True 表示新建，False 表示已存在未覆盖。"""
    if target.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(sample_config(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # 打包模式契约内置在 exe 里，不在数据目录创建空的 contracts/
    for d in ("data", "logs"):
        (target.parent / d).mkdir(parents=True, exist_ok=True)
    if not paths.is_frozen() and not quiet:
        (target.parent / "contracts").mkdir(parents=True, exist_ok=True)
    return True


def ensure_initialized() -> Path:
    """exe 启动时静默确保配置存在（小白无需手敲 init）。返回配置文件路径。"""
    target = paths.config_path()
    _do_init(target, quiet=True)
    return target


def cmd_init(args) -> int:
    home = _project_home()
    target = Path(args.config).expanduser().resolve() if args.config else (home / "config.json")
    if target.exists() and not args.force:
        print(f"配置已存在，未覆盖: {target}（如需覆盖请加 --force）")
        return 1
    if args.force and target.exists():
        target.unlink()
    created = _do_init(target)
    if created:
        print(f"已生成配置模板: {target}")
        if paths.is_frozen():
            print("下一步：双击 exe 打开面板，在「账号配置」里添加账号并配置凭据。")
        else:
            print("下一步：")
            print("  1. 编辑 config.json，为每个账号设置 credential_env（凭据写环境变量，不要写进文件）")
            print("  2. 运行 python -m checkin doctor 检查环境")
            print("  3. 运行 python -m checkin run --simulate 用模板契约验证全流程")
    return 0


def cmd_validate(args) -> int:
    ctx = _context(args)
    cfg, registry = ctx["cfg"], ctx["registry"]
    print_table(["检查项", "结果"], [
        ("配置文件", cfg["_config_path"]),
        ("账号总数", str(len(cfg["accounts"]))),
        ("契约数量", str(len(registry.contracts))),
        ("契约加载错误", str(len(registry.errors))),
        ("数据库", cfg["database"]),
    ])
    if registry.errors:
        print("\n契约错误：")
        for e in registry.errors:
            print(f"  - {e}")
    missing = []
    for acc in cfg["accounts"]:
        if acc.enabled and not registry.has(acc.platform, acc.version):
            missing.append(f"{acc.platform}.{acc.version}")
    if missing:
        print("\n缺少契约的账号：")
        for m in sorted(set(missing)):
            print(f"  - {m}")
    ok = not registry.errors and not missing
    print("\n校验结果:", "通过" if ok else "存在问题，请修复后再运行")
    ctx["storage"].close()
    return 0 if ok else 2


def cmd_doctor(args) -> int:
    ctx = _context(args)
    cfg, registry, storage = ctx["cfg"], ctx["registry"], ctx["storage"]
    rows = config_summary(cfg)
    print_table(["配置项", "值"], rows[1:])
    print("\n账号 / 凭据 / 契约：")
    account_rows = []
    for acc in cfg["accounts"]:
        token_ok = bool(acc.token())
        account_rows.append((
            acc.id, f"{acc.platform}.{acc.version}", acc.name or "-",
            "启用" if acc.enabled else "停用",
            "已注入" if token_ok else "未注入",
            "有" if registry.has(acc.platform, acc.version) else "无",
        ))
    print_table(["账号ID", "平台/版本", "名称", "状态", "凭据", "契约"], account_rows)
    print(f"\n数据库: {cfg['database']}")
    print(f"日志文件: {cfg['log_file']}")
    print(f"契约目录: {cfg['contracts_dir']}")
    if registry.errors:
        print("\n契约加载错误：")
        for e in registry.errors:
            print(f"  - {e}")
    storage.close()
    return 0


def _only_from_args(args) -> Dict[str, str]:
    return {
        "platform": getattr(args, "platform", "") or "",
        "version": getattr(args, "version", "") or "",
        "account_id": getattr(args, "account_id", "") or "",
    }


def cmd_plan(args) -> int:
    ctx = _context(args)
    d = parse_date(args.date, ctx["cfg"]["timezone"])
    tasks = build_daily_plan(ctx["cfg"], ctx["storage"], target_date=d,
                             only=_only_from_args(args),
                             include_catchup=not args.no_catchup)
    print(f"计划日期: {d.isoformat()} | 任务数: {len(tasks)}")
    print_table(["账号ID", "平台", "版本", "目标日期", "类型", "去重键"], [
        (t.account_id, t.platform, t.version, t.target_date.isoformat(),
         "补执行" if t.is_catchup else "当日", t.key) for t in tasks
    ])
    ctx["storage"].close()
    return 0


def cmd_run(args) -> int:
    ctx = _context(args)
    cfg, storage, executor = ctx["cfg"], ctx["storage"], ctx["executor"]
    d = parse_date(args.date, cfg["timezone"])
    # 每次启动都留痕：用于确认计划任务/守护进程确实被触发（即使当天无事可做）
    ctx["logger"].info(
        f"run 启动 | 日期 {d.isoformat()} | 模拟 {bool(args.simulate)} | 强制 {bool(args.force)}")
    tasks = build_daily_plan(cfg, storage, target_date=d, only=_only_from_args(args),
                             include_catchup=not args.no_catchup, include_completed=args.force)
    if not tasks:
        existing = storage.runs_for_date(d.isoformat())
        done = [r for r in existing if r.status in (RunStatus.SUCCESS.value, RunStatus.ALREADY.value)]
        if done:
            msg = f"今天这 {len(done)} 个账号已经签过了，无需重复执行（加 --force 可强制重跑）。"
        else:
            msg = "没有可执行的账号：请确认账号已启用且配好凭据（python -m checkin probe）。"
        ctx["logger"].info(f"run 结束 | 无任务 | {msg}")
        print(msg)
        storage.close()
        return 0
    if args.simulate:
        print("提示：模拟模式运行，模板契约不会产生真实请求。")
    print(f"开始执行 {len(tasks)} 个任务 ...")
    try:
        records = executor.run_tasks(tasks, simulate=args.simulate, force=args.force,
                                     allow_local_decrypt=getattr(args, "allow_local_decrypt", False))
    except RunBusy as exc:
        print(f"已有签到进程在运行（{exc}），本次未执行。")
        storage.close()
        return 3
    print()
    print_table(["账号ID", "平台/版本", "日期", "状态", "次数", "积分", "说明"], [
        (r.account_id, f"{r.platform}.{r.version}", r.target_date,
         _label(r.status), str(r.attempts),
         str(r.credits) if r.credits is not None else "-",
         r.message[:60]) for r in records
    ])
    ctx["notifier"].notify("info", "执行汇总",
                           f"共 {len(records)} 项："
                           + ", ".join(f"{_label(s)}={sum(1 for r in records if r.status == s)}"
                                       for s in dict.fromkeys(r.status for r in records)))
    storage.close()
    return 0 if all(r.status in (RunStatus.SUCCESS.value, RunStatus.ALREADY.value,
                                 RunStatus.SKIPPED.value) for r in records) else 3


def cmd_status(args) -> int:
    ctx = _context(args)
    d = parse_date(args.date, ctx["cfg"]["timezone"])
    runs = ctx["storage"].runs_for_date(d.isoformat())
    print(f"日期: {d.isoformat()} | 记录数: {len(runs)}")
    print_table(["账号ID", "平台/版本", "状态", "次数", "积分", "补跑", "完成时间", "说明"], [
        (r.account_id, f"{r.platform}.{r.version}", _label(r.status), str(r.attempts),
         str(r.credits) if r.credits is not None else "-",
         "是" if r.is_catchup else "否", _short_time(r.finished_at),
         r.message[:50]) for r in runs
    ])
    if runs:
        print("提示：「本次领取」= 这次运行真的发起领取并拿到积分；"
              "「今日已领」= 运行前服务端就显示今天已领过。两者都不会重复领取。")
    ctx["storage"].close()
    return 0


def cmd_history(args) -> int:
    ctx = _context(args)
    runs = ctx["storage"].history(args.limit, platform=args.platform or None,
                                  account_id=args.account_id or None,
                                  status=args.status or None)
    print_table(["账号ID", "平台/版本", "日期", "状态", "次数", "积分", "完成时间", "说明"], [
        (r.account_id, f"{r.platform}.{r.version}", r.target_date, _label(r.status),
         str(r.attempts), str(r.credits) if r.credits is not None else "-",
         _short_time(r.finished_at), r.message[:50]) for r in runs
    ])
    ctx["storage"].close()
    return 0


def cmd_errors(args) -> int:
    ctx = _context(args)
    storage = ctx["storage"]
    runs = storage.history(args.limit, status=RunStatus.FAILED.value) + \
           storage.history(args.limit, status=RunStatus.DRIFT.value)
    print("失败 / 漂移记录：")
    print_table(["账号ID", "平台/版本", "日期", "状态", "次数", "说明"], [
        (r.account_id, f"{r.platform}.{r.version}", r.target_date, _label(r.status),
         str(r.attempts), r.message[:70]) for r in runs
    ])
    print("\n最近尝试日志：")
    for r in runs[:10]:
        attempts = storage.attempts_for(r.key)
        if attempts:
            last = attempts[-1]
            print(f"  - {r.key}: 第{last.attempt}次 {_label(last.status)} {last.message[:80]}")
    storage.close()
    return 0


def cmd_drifts(args) -> int:
    ctx = _context(args)
    events = ctx["storage"].drifts(args.limit)
    print_table(["时间", "平台/版本", "契约版本", "原因"], [
        (_short_time(e.detected_at), f"{e.platform}.{e.version}",
         e.contract_version or "-", e.reason[:80]) for e in events
    ])
    ctx["storage"].close()
    return 0


def cmd_contracts(args) -> int:
    ctx = _context(args)
    registry = ctx["registry"]
    rows = registry.summary()
    print_table(["平台", "版本", "名称", "契约版本", "状态", "步骤数"], [
        (r["platform"], r["version"], r["name"], r["contract_version"], r["status"], r["steps"])
        for r in rows
    ])
    if registry.errors:
        print("\n加载错误：")
        for e in registry.errors:
            print(f"  - {e}")
    ctx["storage"].close()
    return 0


def cmd_probe(args) -> int:
    """探测本机客户端凭据状态（只报存在性，不读取/输出 token 内容）。"""
    from .credentials import probe_local, resolve
    result = probe_local()
    print("本机客户端凭据探测（仅检查文件是否存在，不读取内容）：")
    rows = []
    for platform in ("workbuddy", "trae", "qoder"):
        info = result[platform]
        rows.append((platform, "已找到" if info["found"] else "未找到",
                     "; ".join(info["paths"][:2]) or "-"))
    print_table(["平台", "本地登录态", "路径（最多 2 条）"], rows)
    print(f"\n可选解密依赖 cryptography: {'已安装' if result['cryptography'] else '未安装'}")
    print("\n当前配置账号的凭据解析（脱敏）：")
    ctx = _context(args)
    allow = getattr(args, "allow_local_decrypt", False)
    acc_rows = []
    for acc in ctx["cfg"]["accounts"]:
        if not acc.enabled:
            continue
        cred = resolve(acc, allow_local_decrypt=allow)
        acc_rows.append((acc.id, f"{acc.platform}.{acc.version}", cred.source,
                         cred.masked(), cred.note[:60]))
    print_table(["账号ID", "平台/版本", "来源", "脱敏值", "说明"], acc_rows)
    print("\n提示：真实签到需在环境变量中提供明文 token，例如")
    print("  WORKBUDDY_ACCESS_TOKEN / TRAE_ACCESS_TOKEN / QODER_PAT / QODER_GLOBAL_PAT")
    ctx["storage"].close()
    return 0


def _port_in_use(host: str, port: int) -> bool:
    import socket
    s = socket.socket()
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


def _parse_netstat_pids(text: str, port: int) -> set:
    """从 netstat 输出中解析监听指定端口的 PID（纯函数，便于测试）。"""
    pids = set()
    suffix = f":{port}"
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        local = parts[1]
        if not local.endswith(suffix):
            continue
        if "LISTENING" not in line.upper():
            continue
        pid = parts[-1]
        if pid.isdigit():
            pids.add(int(pid))
    return pids


def _taskkill(pid: int) -> bool:
    """强制结束进程，返回是否成功。"""
    import subprocess
    try:
        r = subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode == 0
    except Exception:
        return False


def _pid_image_name(pid: int) -> str:
    """用 tasklist 取进程映像名。

    比查 WMI 命令行轻、在无控制台的打包环境也更稳。打包后进程名
    ``CreditCheckin*.exe`` 已足够独特，可直接据此认定是本面板。
    """
    import subprocess
    try:
        r = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (r.stdout or b"").decode("mbcs", "replace")
        line = out.strip().splitlines()[0] if out.strip() else ""
        return line.split(",")[0].strip().strip('"')
    except Exception:
        return ""


def _pid_cmdline(pid: int) -> str:
    """查进程命令行。仅对 python 这类通用进程名才需要，用来确认身份。"""
    import subprocess
    try:
        ps = (f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId={pid}' "
              f"-ErrorAction SilentlyContinue; if($p){{Write-Output $p.CommandLine}}")
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return (r.stdout or b"").decode("utf-8", "replace").strip()
    except Exception:
        return ""


def _panel_pids_on_port(port: int) -> set:
    """找出监听端口、且确认是本面板进程的 PID。

    先看进程名（``CreditCheckin*.exe`` 足够独特，不必查命令行）；通用进程名
    （如 python.exe）才核对命令行。绝不按端口无差别结束进程，避免误杀占用
    同一端口的其它开发服务。
    """
    import subprocess
    try:
        proc = subprocess.run(["netstat", "-ano"], capture_output=True, timeout=20,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        return set()
    raw = proc.stdout or b""
    # 中文 Windows 的 netstat 输出是 GBK/MBCS，不能按 utf-8 解码
    out = ""
    for enc in ("mbcs", "gbk", "utf-8"):
        try:
            out = raw.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if not out:
        out = raw.decode("utf-8", errors="ignore")

    pids = {p for p in _parse_netstat_pids(out, port) if p != os.getpid()}
    confirmed = set()
    for pid in pids:
        name = _pid_image_name(pid).lower()
        if name.startswith("creditcheckin") and name.endswith(".exe"):
            confirmed.add(pid)          # 打包模式：按名字即可确认
            continue
        cmd = _pid_cmdline(pid).lower()
        if "-m checkin" in cmd and "web" in cmd:
            confirmed.add(pid)          # 源码模式：python -m checkin web
    return confirmed


def _kill_port_owner(port: int, rounds: int = 3) -> int:
    """结束监听端口且确认为本面板的进程，返回结束数量。

    单次 taskkill 后端口未必立刻释放（onefile 的 bootloader 退出有滞后），
    因此按轮次重试并轮询端口，直到真正空闲，避免新实例绑定失败后
    浏览器仍连在旧进程上（表现为「装了新版却显示旧版本」）。
    """
    if sys.platform != "win32":
        return 0
    killed = 0
    for _ in range(rounds):
        pids = _panel_pids_on_port(port)
        if not pids:
            return killed
        for pid in pids:
            if _taskkill(pid):
                killed += 1
        for _ in range(20):  # 最多等 10 秒
            if not _port_in_use("127.0.0.1", port):
                return killed
            time.sleep(0.5)
    return killed


def _alert(message: str, title: str = "Credit Checkin") -> None:
    """打包成 exe 后没有控制台，print 用户看不到，改用系统弹窗说明失败原因。"""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)  # MB_ICONERROR
    except Exception:
        pass


def cmd_web(args) -> int:
    """启动本地 Web 面板（仅监听 127.0.0.1）。"""
    import webbrowser
    from .web import create_server

    host = "127.0.0.1"  # 强制仅本机，避免凭据/配置被局域网访问
    port = int(args.port)

    if _port_in_use(host, port):
        if getattr(args, "restart", False):
            n = _kill_port_owner(port)
            if n:
                print(f"端口 {port} 被占用，已结束 {n} 个旧进程")
        if _port_in_use(host, port):
            # 仍被占用：可能旧进程迟迟不退出，或占用者是识别不出的其它程序。
            # 绝不能静默失败——用户会以为打开了新版，实际浏览器连的还是旧进程。
            msg = (f"端口 {port} 被占用，无法自动结束旧面板进程。\n\n"
                   "请打开任务管理器，结束所有 CreditCheckin.exe 后重新打开本程序。\n"
                   "（若该端口是其它软件在用，可换端口启动。）")
            print(msg, file=sys.stderr)
            _alert(msg)
            return 2

    ctx = _context(args)
    if args.simulate:
        ctx["registry"].simulate = True
    # exe 面板启动时修正「定时任务指向源码 python」的历史遗留问题，
    # 否则签到写到另一个数据库，页面看起来像没签到。失败不影响面板启动。
    if paths.is_frozen():
        try:
            from .manage import repair_scheduled_task
            fixed = repair_scheduled_task(ctx["config_path"])
            if fixed:
                ctx["logger"].warning(f"已自动修正定时任务程序路径: {fixed}")
        except Exception as exc:
            ctx["logger"].warning(f"定时任务自检失败（不影响面板）: {exc}")
    try:
        server = create_server(ctx, host=host, port=port,
                               allow_local_decrypt=args.allow_local_decrypt)
    except OSError as exc:
        print(f"端口 {port} 启动失败: {exc}（可用 --port 换一个端口）", file=sys.stderr)
        ctx["storage"].close()
        return 2
    url = f"http://{host}:{port}/"
    print(f"面板已启动: {url}")
    print(f"后端 API 版本: {__import__('checkin.web', fromlist=['API_VERSION']).API_VERSION}")
    print("仅监听本机 127.0.0.1。按 Ctrl+C 停止。")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n面板已停止")
    finally:
        server.server_close()
        ctx["storage"].close()
    return 0


def cmd_schedule(args) -> int:
    """守护模式：按间隔轮询，仅在签到窗口内执行计划。"""
    ctx = _context(args)
    cfg, storage, executor, logger = ctx["cfg"], ctx["storage"], ctx["executor"], ctx["logger"]
    interval = max(1, int(args.interval_minutes)) * 60
    logger.info(f"守护模式启动：每 {args.interval_minutes} 分钟检查一次，窗口 "
                f"{cfg['checkin_window']['start']}~{cfg['checkin_window']['end']} ({cfg['timezone']})")
    try:
        while True:
            now = now_local(cfg["timezone"])
            if in_checkin_window(cfg, now):
                d = now.date()
                tasks = build_daily_plan(cfg, storage, target_date=d)
                if tasks:
                    logger.info(f"窗口内发现 {len(tasks)} 个待执行任务")
                    executor.run_tasks(tasks, simulate=args.simulate,
                                       allow_local_decrypt=getattr(args, "allow_local_decrypt", False))
                else:
                    logger.debug("窗口内无待执行任务")
            else:
                logger.debug("当前不在签到窗口内，跳过")
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n已停止守护模式")
    finally:
        storage.close()
    return 0


# ---------------- 参数解析 ----------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="checkin",
        description="个人多平台（WorkBuddy / Trae / Qoder，国内版与国际版）每日签到领积分管理工具",
    )
    parser.add_argument("--config", help="配置文件路径（默认 <项目根>/config.json）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="生成配置模板")
    p.add_argument("--force", action="store_true", help="覆盖已有配置")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("validate", help="校验配置与契约")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("doctor", help="查看脱敏后的环境与账号概览")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("plan", help="查看今日/指定日期的签到计划")
    p.add_argument("--date", help="日期 YYYY-MM-DD，默认今天")
    p.add_argument("--platform", help="仅显示某平台")
    p.add_argument("--version", help="仅显示某版本 cn/global")
    p.add_argument("--account-id", help="仅显示某账号")
    p.add_argument("--no-catchup", action="store_true", help="不包含补执行任务")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("run", help="执行签到（默认跳过已完成任务）")
    p.add_argument("--date", help="日期 YYYY-MM-DD，默认今天")
    p.add_argument("--platform", help="仅运行某平台")
    p.add_argument("--version", help="仅运行某版本 cn/global")
    p.add_argument("--account-id", help="仅运行某账号")
    p.add_argument("--no-catchup", action="store_true", help="不包含补执行任务")
    p.add_argument("--simulate", "--dry-run", action="store_true", help="模拟运行（不发起真实请求）")
    p.add_argument("--force", action="store_true", help="忽略去重，强制执行")
    p.add_argument("--allow-local-decrypt", action="store_true",
                   help="允许尝试从本地客户端登录态解析凭据")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("status", help="查看某日执行状态")
    p.add_argument("--date", help="日期 YYYY-MM-DD，默认今天")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("history", help="查看历史记录")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--platform")
    p.add_argument("--account-id")
    p.add_argument("--status", help="按状态过滤，如 success/failed/drift")
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("errors", help="查看失败与异常")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_errors)

    p = sub.add_parser("drifts", help="查看契约漂移事件")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_drifts)

    p = sub.add_parser("probe", help="探测本机客户端凭据与环境变量状态（脱敏）")
    p.add_argument("--allow-local-decrypt", action="store_true",
                   help="允许尝试从本地客户端登录态解析凭据")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("contracts", help="查看已加载契约")
    p.set_defaults(func=cmd_contracts)

    p = sub.add_parser("web", help="启动本地 Web 面板（仅 127.0.0.1）")
    p.add_argument("--port", type=int, default=8765, help="端口，默认 8765")
    p.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    p.add_argument("--restart", action="store_true", help="端口被占用时自动结束旧面板进程")
    p.add_argument("--simulate", action="store_true", help="模拟模式，不发起真实请求")
    p.add_argument("--allow-local-decrypt", action="store_true",
                   help="允许面板尝试从本地客户端登录态解析凭据")
    p.set_defaults(func=cmd_web)

    p = sub.add_parser("schedule", help="守护模式：窗口内定时执行")
    p.add_argument("--interval-minutes", type=int, default=30)
    p.add_argument("--simulate", action="store_true")
    p.add_argument("--allow-local-decrypt", action="store_true")
    p.set_defaults(func=cmd_schedule)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "init":
        return cmd_init(args)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已中断")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
