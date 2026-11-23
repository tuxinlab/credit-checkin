"""程序入口：源码模式走标准 CLI；打包后的 exe 提供小白友好的双模式。

- 双击 exe（无参数）：自动初始化配置 → 启动 Web 面板并打开浏览器
- ``CreditCheckin.exe --run``：静默执行一次签到（供 Windows 计划任务调用）
- 其它参数：原样交给 CLI（如 ``--status``、``run --simulate`` 等，供高级用户使用）
"""
from __future__ import annotations

import sys

from . import paths
from .cli import main as cli_main


def _exe_main(argv: list[str]) -> int:
    """打包 exe 的入口分流。"""
    # 首次运行静默生成配置（小白不用手敲 init）
    from .cli import ensure_initialized
    ensure_initialized()

    if not argv:
        # 双击打开：启动面板
        return cli_main(["web", "--restart"])

    if argv[0] == "--run":
        # 计划任务调用：静默签到。结果写入日志与数据库，面板可查看。
        # --noconsole 下没有控制台，未捕获异常必须落盘，否则用户完全看不到失败原因。
        try:
            return cli_main(["run", *argv[1:]])
        except Exception as exc:  # noqa: BLE001 - 兜底一切，保证静默模式不无声失败
            _log_fatal(exc)
            return 1

    # 高级用户仍可用 CLI，把 exe 当成命令行工具
    return cli_main(argv)


def _log_fatal(exc: BaseException) -> None:
    """静默模式崩溃兜底：写到 data/logs/checkin.log，面板「异常」页能看到。"""
    import traceback
    from datetime import datetime
    try:
        from . import paths
        log_dir = paths.data_root() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        line = (f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] "
                f"[FATAL] exe --run 未捕获异常: {type(exc).__name__}: {exc}\n"
                f"{traceback.format_exc()}\n")
        with (log_dir / "checkin.log").open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


def entry() -> int:
    # Windows 打包后强制 UTF-8 输出，避免中文在 GBK 控制台里乱码
    if paths.is_frozen():
        for stream in (sys.stdout, sys.stderr):
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure:
                try:
                    reconfigure(encoding="utf-8")
                except (OSError, ValueError):
                    pass
    argv = list(sys.argv[1:])
    if paths.is_frozen():
        return _exe_main(argv)
    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(entry())
