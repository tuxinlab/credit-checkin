"""路径解析：同时支持源码运行与 PyInstaller 打包后的 exe 运行。

两类路径要分清：
- **只读资源**（contracts/、web/index.html）：随代码发布。源码时在仓库根，
  打包后由 PyInstaller 内置到 ``sys._MEIPASS`` 解压目录。
- **可写数据**（config.json、data/、logs/）：运行时生成。优先级：
  1. 环境变量 ``CHECKIN_HOME``（显式指定，最高，测试用）
  2. **打包后固定用** ``%LOCALAPPDATA%/CreditCheckin``——exe 放在哪都读同一份数据，
     避免用户移动 exe 后"数据消失"
  3. 源码运行用仓库根（开发与正式数据隔离）

打包首次启动时若固定目录还没有数据，会自动从 exe 同目录迁移旧版本数据
（见 :func:`migrate_legacy_data`），避免升级后看起来"数据全丢了"。

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

    # 打包后固定用用户目录：exe 放哪都是同一份数据
    if is_frozen():
        root = _app_dir_fallback()
        root.mkdir(parents=True, exist_ok=True)
        return _set_cache(root)

    # 源码运行：仓库根（开发方便），不可写时降级用户目录
    base = Path(__file__).resolve().parents[1]
    if _dir_writable(base):
        return _set_cache(base)
    fallback = _app_dir_fallback()
    fallback.mkdir(parents=True, exist_ok=True)
    return _set_cache(fallback)


def legacy_data_dirs() -> list[Path]:
    """旧版本可能存放数据的目录（仅 exe 同目录），用于首次迁移探测。"""
    if not is_frozen():
        return []
    return [Path(sys.executable).resolve().parent]


def migrate_legacy_data() -> Optional[Path]:
    """打包首次启动时，把 exe 同目录的旧数据复制到固定数据目录。

    只复制、不删除原文件（原目录可能只读或用户还想留档）。
    返回迁移来源目录；无需迁移返回 None。
    """
    if not is_frozen():
        return None
    import shutil
    target = data_root()
    if (target / "config.json").exists():
        return None
    for src in legacy_data_dirs():
        if src == target or not (src / "config.json").is_file():
            continue
        try:
            shutil.copy2(src / "config.json", target / "config.json")
            for name in ("data", "logs"):
                sub = src / name
                if sub.is_dir():
                    shutil.copytree(sub, target / name, dirs_exist_ok=True)
        except OSError:
            continue
        return src
    return None


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
