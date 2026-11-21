@echo off
setlocal
cd /d "%~dp0"
title AutoLog Launcher

echo ======================================================================
echo    AutoLog Studio
echo ======================================================================
echo.

:: 1. Search for Python executable
set "PY_CMD="

if exist "C:\Users\%USERNAME%\AppData\Local\Programs\Python\Python312\python.exe" (
    set "PY_CMD=C:\Users\%USERNAME%\AppData\Local\Programs\Python\Python312\python.exe"
    goto :PYTHON_FOUND
)

where python >nul 2>&1
if %ERRORLEVEL% equ 0 (
    set "PY_CMD=python"
    goto :PYTHON_FOUND
)

where py >nul 2>&1
if %ERRORLEVEL% equ 0 (
    set "PY_CMD=py -3"
    goto :PYTHON_FOUND
)

if exist "C:\Python312\python.exe" (
    set "PY_CMD=C:\Python312\python.exe"
    goto :PYTHON_FOUND
)

echo [ERROR] Python not found on your system!
echo Please install Python 3.10+ from https://www.python.org
echo (Make sure to check "Add python.exe to PATH" during installation)
echo.
pause
exit /b 1

:PYTHON_FOUND
echo [1/3] Python detected: %PY_CMD%

:: 2. Check required dependencies
echo [2/3] Checking required libraries (playwright, requests, pillow, keyring)...
"%PY_CMD%" -c "import playwright, requests, PIL, keyring, google.genai, importlib.metadata as m, sys; sys.exit(0 if tuple(map(int, m.version('playwright').split('.')[:2])) >= (1, 64) else 1)" >nul 2>&1
if %ERRORLEVEL% equ 0 (
    echo [OK] All Python packages are already installed.
    goto :CHECK_BROWSER
)

echo [!] Installing required Python packages...
"%PY_CMD%" -m pip install --upgrade pip
"%PY_CMD%" -m pip install --upgrade -r requirements.txt
if %ERRORLEVEL% neq 0 (
    echo [WARNING] Failed to install packages via pip.
)

:CHECK_BROWSER
echo [3/3] Checking Playwright Chromium browser...
"%PY_CMD%" -c "from playwright.sync_api import sync_playwright; pw=sync_playwright().start(); p=pw.chromium.executable_path; pw.stop(); exit(0 if p else 1)" >nul 2>&1
if %ERRORLEVEL% equ 0 (
    echo [OK] Chromium browser is ready.
    goto :LAUNCH_APP
)

echo [!] Installing Playwright Chromium browser...
"%PY_CMD%" -m playwright install chromium

:LAUNCH_APP
echo.
echo ======================================================================
echo    Starting Web Dashboard at http://127.0.0.1:8765
echo ======================================================================
echo.

start http://127.0.0.1:8765
"%PY_CMD%" app.py

echo.
echo Server stopped.
pause
