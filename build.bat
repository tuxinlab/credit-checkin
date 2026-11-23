@echo off
chcp 65001 >nul
title Build CreditCheckin exe
cd /d "%~dp0"

echo ============================================
echo  CreditCheckin - build single-file exe
echo ============================================
echo.

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

echo [1/3] Checking PyInstaller...
%PY% -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
  echo Installing PyInstaller (one-time)...
  %PY% -m pip install pyinstaller
  if errorlevel 1 (
    echo Default index had no package, retrying official PyPI...
    %PY% -m pip install pyinstaller -i https://pypi.org/simple
  )
  %PY% -m PyInstaller --version >nul 2>&1
  if errorlevel 1 (
    echo [ERROR] Failed to install PyInstaller.
    pause
    exit /b 1
  )
)

echo [2/3] Building exe (this may take a few minutes)...
%PY% -m PyInstaller ^
  --onefile ^
  --noconsole ^
  --name "CreditCheckin" ^
  --add-data "contracts;contracts" ^
  --add-data "web;web" ^
  --hidden-import checkin.web ^
  --clean ^
  --noconfirm ^
  main.py
if errorlevel 1 (
  echo [ERROR] Build failed.
  pause
  exit /b 1
)

echo [3/3] Done.
echo.
echo   exe: dist\CreditCheckin.exe
echo.
echo   Copy dist\CreditCheckin.exe anywhere, double-click to open the panel.
echo   First run creates config.json and data\ next to the exe.
echo.
pause
