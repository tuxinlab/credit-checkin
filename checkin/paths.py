"""路径解析：同时支持源码运行与 PyInstaller 打包后的 exe 运行。

两类路径要分清：
- **只读资源**（contracts/、web/index.html）：随代码发布。源码时在仓库根，
  打包后由 PyInstaller 内置到 ``sys._MEIPASS`` 解压目录。
- **可写数据**（config.json、data/、logs/）：运行时生成。优先级：
  1. 环境变量 ``CHECKIN_HOME``（显式指定，最高）
  2. exe / 源码根目录（绿色便携，用户看得见）
  3. 目录不可写时降级到 ``%LOCALAPPDATA%/CreditCheckin``（如 exe 放在 Program Files）

所有其它模块都应通过这里取路径，不要再直接用 ``__file__.parents[1]``。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Optional

APP_DIR_NAME = "CreditCheckin"


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包环境。"""
    return bool(getattr(sys, "frozen", False))


def _bundle_root() -> Path:
    """只读资源根目录（源码仓库根 / 打包解压目录）。"""
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass).resolve()
    return Path(__file__).resolve().parents[1]


def resource_path(relative: str) -> Path:
    """内置资源路径，如 ``resource_path('contracts')`` / ``resource_path('web/index.html')``。"""
    return _bundle_root() / relative


def contracts_dir() -> Path:
    return resource_path("contracts")


def static_dir() -> Path:
    return resource_path("web")


def _dir_writable(path: Path) -> bool:
    """探测目录是否可写（真实创建临时文件，而非只看权限位）。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=str(path), prefix=".writable-", delete=True):
            pass
        return True
    except (OSError, PermissionError):
        return False


def _app_dir_fallback() -> Path:
    """Windows 用 LOCALAPPDATA，其它平台用 ~/.local/share。"""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base).expanduser().resolve() / APP_DIR_NAME


def data_root() -> Path:
    """可写数据根目录，返回已确认可写的路径。结果缓存，避免每次探测。

    缓存会跟随 CHECKIN_HOME 变化失效（测试或运行时切换目录都能生效）。
    """
    env = os.environ.get("CHECKIN_HOME")
    cached = getattr(data_root, "_cache", None)
    cached_env = getattr(data_root, "_cache_env", None)
    if cached is not None and cached_env == env:
        return cached

    def _set_cache(value: Path) -> Path:
        data_root._cache = value
        data_root._cache_env = env
        return value

    if env:
        root = Path(env).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        return _set_cache(root)

    # exe 所在目录（打包）或仓库根（源码）
    if is_frozen():
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parents[1]

    if _dir_writable(base):
        return _set_cache(base)
    fallback = _app_dir_fallback()
    fallback.mkdir(parents=True, exist_ok=True)
    return _set_cache(fallback)


def init_data_dirs() -> Path:
    """创建数据子目录（data/、logs/），返回数据根目录。"""
    root = data_root()
    for name in ("data", "logs"):
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def config_path() -> Path:
    return data_root() / "config.json"


def reset_cache() -> None:
    """测试用：清除数据目录缓存（环境变量变更后需调用）。"""
    data_root._cache = None
    data_root._cache_env = None
