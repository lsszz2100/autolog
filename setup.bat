@echo off
setlocal
cd /d "%~dp0"
title AutoLog Setup

echo ======================================================================
echo    AutoLog Studio - Setup & Dependencies
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
echo.

:: 2. Install pip packages
echo [2/3] Installing Python libraries (playwright, requests, pillow)...
"%PY_CMD%" -m pip install --upgrade pip
"%PY_CMD%" -m pip install -r requirements.txt
if %ERRORLEVEL% equ 0 (
    echo [OK] Python libraries installed successfully!
) else (
    echo [WARNING] Some errors occurred during pip install.
)
echo.

:: 3. Install Playwright browser
echo [3/3] Installing Playwright Chromium browser...
"%PY_CMD%" -m playwright install chromium
if %ERRORLEVEL% equ 0 (
    echo [OK] Chromium browser installed successfully!
) else (
    echo [WARNING] Some errors occurred during Chromium installation.
)

echo.
echo ======================================================================
echo    Setup completed! You can now run 'run.bat' to start the studio.
echo ======================================================================
echo.
pause
