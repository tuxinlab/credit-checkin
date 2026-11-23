@echo off
chcp 65001 >nul
title Credit Checkin 面板
cd /d "%~dp0"

set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
  where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo [错误] 未找到 Python 3。请先安装 Python 3.9+ 并加入 PATH。
  pause
  exit /b 1
)

echo 正在启动 Credit Checkin 面板...
echo 浏览器会自动打开 http://127.0.0.1:8765
echo 关闭本窗口即可停止面板。
echo.

%PY% -m checkin web --port 8765 --restart
if errorlevel 1 (
  echo.
  echo 面板启动失败。请检查 config.json 是否存在（运行 python -m checkin init 生成）。
  pause
)
