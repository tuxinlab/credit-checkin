"""PyInstaller 打包入口：源码模式不用这个文件，仅 exe 构建时作为顶层脚本。"""
from checkin.__main__ import entry

if __name__ == "__main__":
    raise SystemExit(entry())
