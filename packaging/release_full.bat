@echo off
rem [전체 배포] 설치 파일까지 새로 빌드해 배포 — 라이브러리·Chromium·launcher.py 가 바뀌었을 때, 또는 신규 설치 파일 갱신용
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0.."
set "PY=%CD%\.build-venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
call packaging\build_installer.bat || goto :fail
"%PY%" packaging\make_release.py full || goto :fail
exit /b 0
:fail
echo [실패] 배포 중 오류가 발생했습니다.
exit /b 1
