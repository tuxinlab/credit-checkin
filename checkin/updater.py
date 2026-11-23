"""版本更新检查与一键替换。

设计原则：
- **检查静默、替换需用户确认**：检查失败不影响签到；替换必须由用户点击确认，
  绝不后台静默替换（开源工具静默执行下载文件风险高、易被杀毒误报）。
- **只从本仓库下载**：下载地址必须属于 ``GITHUB_REPO`` 的 releases，防止被劫持。
- **可回滚**：替换前备份旧 exe，失败时还原。
- **缓存结果**：面板轮询时不会每次都请求 GitHub（默认 1 小时）。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from . import __version__

# ---- 配置：用户开源后改成自己的 GitHub 仓库 ----
GITHUB_REPO = "tuxinlab/credit-checkin"
# 优先用 releases.atom（github.com 域名，不受 API 速率限制，无需认证）；
# 匿名 API 每小时仅 60 次/IP，共享出口（公司/校园网）很容易撞限额。
RELEASES_ATOM = "https://github.com/{repo}/releases.atom"
RELEASES_API = "https://api.github.com/repos/{repo}/releases/latest"
CHECK_INTERVAL_SECONDS = 3600  # 缓存 1 小时
HTTP_TIMEOUT = 6


@dataclass
class UpdateInfo:
    current: str
    latest: str = ""
    has_update: bool = False
    release_url: str = ""
    download_url: str = ""
    notes: str = ""
    checked_at: float = 0.0
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "current": self.current,
            "latest": self.latest,
            "has_update": self.has_update,
            "release_url": self.release_url,
            "download_url": self.download_url,
            "notes": self.notes[:500],
            "error": self.error,
        }


_cache: Optional[UpdateInfo] = None


def _configured() -> bool:
    return bool(GITHUB_REPO) and "/" in GITHUB_REPO


def _version_key(value: str) -> tuple:
    """把版本号转成可比较的元组：'0.1.2' -> (0, 1, 2)。非数字段按 0 处理。"""
    parts = re.findall(r"\d+", str(value or ""))
    return tuple(int(p) for p in parts) if parts else (0,)


def _is_newer(latest: str, current: str) -> bool:
    return _version_key(latest) > _version_key(current)


def _pick_asset(release: Dict[str, Any]) -> str:
    """优先选 Windows exe 资产（.exe），否则给 releases 页面。"""
    for asset in (release.get("assets") or []):
        name = str(asset.get("name", "")).lower()
        if name.endswith(".exe"):
            return str(asset.get("browser_download_url", "") or "")
    return str(release.get("html_url", "") or "")


def _fetch_atom() -> UpdateInfo:
    """从 releases.atom 解析最新版本。github.com 域名，不受 API 速率限制。"""
    import xml.etree.ElementTree as ET
    info = UpdateInfo(current=__version__, checked_at=time.time())
    url = RELEASES_ATOM.format(repo=GITHUB_REPO)
    req = urllib.request.Request(
        url, headers={"Accept": "application/atom+xml",
                      "User-Agent": "credit-checkin-updater"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        xml_bytes = resp.read()
    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(xml_bytes)
    entry = root.find("a:entry", ns)
    if entry is None:
        raise ValueError("feed 中没有 release")
    link_el = entry.find("a:link", ns)
    href = str(link_el.get("href", "")) if link_el is not None else ""
    matched = re.search(r"/releases/tag/([^/]+)/?$", href)
    if not matched:
        raise ValueError("无法从 feed 解析 tag")
    raw_tag = matched.group(1)
    tag = raw_tag.lstrip("v")
    info.latest = tag
    info.release_url = href
    info.download_url = (f"https://github.com/{GITHUB_REPO}"
                         f"/releases/download/{raw_tag}/CreditCheckin.exe")
    title_el = entry.find("a:title", ns)
    info.notes = (title_el.text or "") if title_el is not None else ""
    info.has_update = bool(tag) and _is_newer(tag, __version__)
    return info


def _fetch_api() -> UpdateInfo:
    """GitHub Releases API（能拿到详细说明，但匿名请求有速率限制）。"""
    info = UpdateInfo(current=__version__, checked_at=time.time())
    url = RELEASES_API.format(repo=GITHUB_REPO)
    req = urllib.request.Request(
        url, headers={"Accept": "application/vnd.github+json",
                      "User-Agent": "credit-checkin-updater"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    tag = str(data.get("tag_name", "") or "").lstrip("v")
    info.latest = tag
    info.release_url = str(data.get("html_url", "") or "")
    info.download_url = _pick_asset(data)
    info.notes = str(data.get("body", "") or "")
    info.has_update = bool(tag) and _is_newer(tag, __version__)
    return info


def _fetch() -> UpdateInfo:
    info = UpdateInfo(current=__version__, checked_at=time.time())
    if not _configured():
        info.error = "未配置 GitHub 仓库地址"
        return info
    errors = []
    for name, fetcher in (("atom", _fetch_atom), ("api", _fetch_api)):
        try:
            return fetcher()
        except Exception as exc:  # 网络/限流/解析失败：换下一个，都失败则静默降级
            errors.append(f"{name}:{type(exc).__name__}")
    info.error = " / ".join(errors)
    return info


def check_update(*, force: bool = False) -> UpdateInfo:
    """检查更新，带缓存。force=True 忽略缓存（用于手动点"检查更新"）。"""
    global _cache
    now = time.time()
    if (not force and _cache is not None
            and now - _cache.checked_at < CHECK_INTERVAL_SECONDS):
        return _cache
    _cache = _fetch()
    return _cache


def clear_cache() -> None:
    global _cache
    _cache = None


# ---------------- 一键替换 ----------------
MAX_DOWNLOAD_BYTES = 150 * 1024 * 1024  # 远大于实际 11MB，防异常大文件

# 更新进度（供面板轮询）。下载在后台线程进行，避免长请求把界面卡住，
# 也让用户能看到百分比而不是干等。
_progress: Dict[str, Any] = {
    "phase": "idle",        # idle / downloading / verifying / replacing / restarting / error
    "downloaded": 0,
    "total": 0,
    "latest": "",
    "error": "",
    "updated_at": 0.0,
}
_progress_lock = threading.Lock()
_apply_lock = threading.Lock()
_apply_thread: Optional[threading.Thread] = None


def _set_progress(**kwargs: Any) -> None:
    with _progress_lock:
        _progress.update(kwargs)
        _progress["updated_at"] = time.time()


def download_progress() -> Dict[str, Any]:
    """当前更新进度快照。"""
    with _progress_lock:
        return dict(_progress)


def _is_allowed_download(url: str) -> bool:
    """只接受本仓库 releases 的下载地址，避免更新被指向任意地址。"""
    return url.startswith(f"https://github.com/{GITHUB_REPO}/releases/download/")


def download_update(info: "UpdateInfo", dest_dir: Path) -> Path:
    """下载新版本 exe 并校验 PE 文件头，返回落盘路径。"""
    url = info.download_url
    if not info.has_update or not url:
        raise RuntimeError("没有可用的新版本")
    if not _is_allowed_download(url):
        raise RuntimeError("下载地址不属于本仓库，已拒绝")
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    tmp = dest_dir / "CreditCheckin.download"
    final = dest_dir / "CreditCheckin.new.exe"
    downloaded = 0
    req = urllib.request.Request(url, headers={"User-Agent": "credit-checkin-updater"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        try:
            declared = int(resp.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            declared = 0  # GitHub 走 302 跳转，个别情况下拿不到总长度
        _set_progress(phase="downloading", downloaded=0, total=declared, error="")
        with tmp.open("wb") as fh:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                downloaded += len(chunk)
                if downloaded > MAX_DOWNLOAD_BYTES:
                    fh.close()
                    tmp.unlink(missing_ok=True)
                    raise RuntimeError("下载文件异常偏大，已中止")
                fh.write(chunk)
                _set_progress(downloaded=downloaded)
    _set_progress(phase="verifying")
    with tmp.open("rb") as fh:
        magic = fh.read(2)
    if magic != b"MZ":  # 必须在关闭句柄后删除，否则 Windows 下报占用
        tmp.unlink(missing_ok=True)
        raise RuntimeError("下载内容不是有效的 Windows 程序，已中止")
    tmp.replace(final)
    return final


def build_apply_script(target: Path, new_file: Path, pid: int) -> Path:
    """生成替换脚本：等面板退出 → 备份旧 exe → 覆盖 → 启动新版 → 清理。

    用系统本地编码（mbcs/GBK）写批处理，保证含中文的路径也能正确解析。
    """
    script = target.parent / "_apply_update.bat"
    # 用「能否把旧 exe 改名」判断文件是否已释放，比查 PID 更可靠：
    # onefile 的 bootloader 可能在子进程退出后仍短暂持有文件句柄。
    content = f"""@echo off
setlocal enabledelayedexpansion
rem 清除 PyInstaller 运行时环境变量：新进程若继承旧的解压目录(_MEIxxxx)
rem 会因该目录已被上一进程清理而启动失败
set "_MEIPASS2="
set "_PYI_APPLICATION_HOME_DIR="
set "_PYI_ARCHIVE_FILE="
set "_PYI_PARENT_PROCESS_LEVEL="
set "_PYI_SPLASH_IPC="
set "OLD={target}"
set "NEW={new_file}"
set "BAK={target}.bak"

set /a n=0
:waitfree
if exist "%BAK%" del /f /q "%BAK%" >nul 2>&1
move /y "%OLD%" "%BAK%" >nul 2>&1
if errorlevel 1 (
  set /a n+=1
  if !n! geq 90 goto giveup
  ping -n 2 127.0.0.1 >nul
  goto waitfree
)

move /y "%NEW%" "%OLD%" >nul 2>&1
if errorlevel 1 goto restore

start "" "%OLD%"
ping -n 3 127.0.0.1 >nul
del /f /q "%BAK%" >nul 2>&1
del /f /q "%NEW%" >nul 2>&1
del /f /q "%~f0" >nul 2>&1
exit /b 0

:restore
if exist "%BAK%" move /y "%BAK%" "%OLD%" >nul 2>&1
if exist "%OLD%" start "" "%OLD%"
exit /b 1

:giveup
if exist "%NEW%" del /f /q "%NEW%" >nul 2>&1
if exist "%OLD%" start "" "%OLD%"
exit /b 2
"""
    # cmd 解析 .bat 必须用 CRLF；LF-only 会把所有命令拼成一行而静默失效。
    script.write_bytes(content.replace("\n", "\r\n").encode("mbcs", "replace"))
    return script


def apply_update(info: "UpdateInfo", data_dir: Path) -> Dict[str, Any]:
    """启动后台更新流程并立即返回，进度通过 :func:`download_progress` 查询。

    面板是 ThreadingHTTPServer，下载放到后台线程，前端才能轮询到进度；
    否则长请求会让界面长时间无反馈，用户无法判断是卡住还是在下载。
    """
    from . import paths
    global _apply_thread
    if not paths.is_frozen():
        return {"ok": False, "message": "源码模式请用 git pull 更新，不支持自动替换"}
    if not info.has_update:
        return {"ok": False, "message": "当前已是最新版本"}
    with _apply_lock:
        if _apply_thread is not None and _apply_thread.is_alive():
            return {"ok": False, "message": "更新正在进行中，请稍候"}
        _apply_thread = threading.Thread(
            target=_apply_worker, args=(info, Path(data_dir)), daemon=True)
        _apply_thread.start()
    return {"ok": True, "started": True, "latest": info.latest,
            "message": f"开始下载 v{info.latest}…"}


def _apply_worker(info: "UpdateInfo", data_dir: Path) -> None:
    """后台流程：下载 → 校验 → 生成替换脚本 → 启动脚本 → 退出本进程。"""
    try:
        target = Path(sys.executable).resolve()
        new_file = download_update(info, data_dir / "update")
        script = build_apply_script(target, new_file, os.getpid())
        _set_progress(phase="replacing")
        # 直接 cmd /c 调用脚本，不要用 start：无控制台环境下 start 会挂起，
        # 且会把空标题 "" 误当成要打开的文件。Popen 不等待，退出后脚本继续跑。
        subprocess.Popen(
            ["cmd", "/c", str(script)],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            close_fds=True, cwd=str(script.parent),
        )
        _set_progress(phase="restarting")
        time.sleep(1.2)  # 给前端留出读到最终状态并提示的时间
        os._exit(0)
    except Exception as exc:  # noqa: BLE001 - 把失败原因原样告诉用户
        _set_progress(phase="error", error=str(exc))
