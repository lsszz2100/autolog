@echo off
rem Open the (private) release repo briefly so THIS PC can install the latest update, then close it.
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0.."
set "PY=%CD%\.build-venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
"%PY%" packaging\make_release.py open
exit /b %ERRORLEVEL%
