@echo off
chcp 65001 >nul
title Credit Checkin Panel
cd /d "%~dp0"

set "PORT=8765"
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
  where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo [ERROR] Python 3 not found. Install Python 3.9+ and add to PATH.
  pause
  exit /b 1
)

echo Starting Credit Checkin panel on http://127.0.0.1:%PORT%
echo Close this window to stop the panel.
echo.

rem --restart：若端口被旧面板占用，自动结束旧进程，避免浏览器连到旧后端（前端新、后端旧会报 not found）
%PY% -m checkin web --port %PORT% --restart
if errorlevel 1 (
  echo.
  echo Failed to start. Check config.json exists: python -m checkin init
  pause
)
