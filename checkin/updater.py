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
