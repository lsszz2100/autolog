@echo off
rem [평소 배포] 바뀐 프로그램 코드만 배포 (빌드 없음, 1분 이내)
rem   1) version.py 의 APP_VERSION 올리기  2) (선택) packaging\release_notes.txt 에 변경 내용  3) 이 파일 실행
rem 라이브러리(requirements.txt)·spec·launcher.py 를 바꿨다면 REQUIRED_RUNTIME 을 올리고 release_full.bat 사용
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0.."
set "PY=%CD%\.build-venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
"%PY%" packaging\make_release.py code || goto :fail
exit /b 0
:fail
echo [실패] 배포 중 오류가 발생했습니다.
exit /b 1
