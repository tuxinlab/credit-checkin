"""版本更新检查：从 GitHub Releases 读取最新版本，与本地版本比对。

设计原则：
- **只提示、不自动下载执行**：个人开源工具，自动替换 exe 风险高且易被杀毒误报。
- **失败静默**：无网络、仓库未配置、API 限流都不影响签到主流程。
- **缓存结果**：面板轮询时不会每次都请求 GitHub（默认 1 小时）。
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional

from . import __version__

# ---- 配置：用户开源后改成自己的 GitHub 仓库 ----
GITHUB_REPO = "tuxinlab/credit-checkin"
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


def _fetch() -> UpdateInfo:
    info = UpdateInfo(current=__version__, checked_at=time.time())
    if not _configured():
        info.error = "未配置 GitHub 仓库地址"
        return info
    url = RELEASES_API.format(repo=GITHUB_REPO)
    try:
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
    except Exception as exc:  # 网络/API/解析失败一律静默降级
        info.error = f"{type(exc).__name__}"
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
