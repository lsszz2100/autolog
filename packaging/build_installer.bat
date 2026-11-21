@echo off
setlocal
rem AutoLog 설치 프로그램 빌드
rem   1) 빌드 전용 가상환경(.build-venv) 생성 및 패키지 설치
rem   2) PyInstaller로 dist\NaverBlogStudio 생성
rem   3) Chromium을 dist\NaverBlogStudio\ms-playwright 에 내려받기 (사용자 PC 크롬 불필요)
rem   4) Inno Setup으로 packaging\output\NaverBlogStudio-Setup-<버전>.exe 생성
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0.."
set "ROOT=%CD%"
set "VENV=%ROOT%\.build-venv"

set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" (
  where py >nul 2>&1 && set "PY=py -3.12"
)

echo [1/4] 빌드 가상환경 준비...
if not exist "%VENV%\Scripts\python.exe" (
  %PY% -m venv "%VENV%" || goto :fail
)
"%VENV%\Scripts\python.exe" -m pip install --upgrade pip >nul
"%VENV%\Scripts\python.exe" -m pip install -r requirements.txt pyinstaller cython setuptools || goto :fail

rem 버전: version.py 단일 기준 → Windows 파일 버전 정보·설치 프로그램 버전에 반영
"%VENV%\Scripts\python.exe" packaging\make_version_info.py || goto :fail
set /p APPVER=<packaging\appver.txt
if "%APPVER%"=="" goto :fail
echo     버전: %APPVER%

echo [2/4] 코드 기계어 컴파일(리버싱 방지) + PyInstaller 빌드...
"%VENV%\Scripts\python.exe" packaging\compile_code.py || goto :fail
if exist "%ROOT%\dist\NaverBlogStudio" rmdir /s /q "%ROOT%\dist\NaverBlogStudio"
rem --clean 생략: 이전 분석 캐시(build\) 재사용으로 빌드 시간 단축
"%VENV%\Scripts\python.exe" -m PyInstaller --noconfirm --distpath "%ROOT%\dist" --workpath "%ROOT%\build" packaging\AutoLog.spec || goto :fail

echo [3/4] Chromium 포함...
rem 한 번 받은 Chromium은 packaging\.chromium-cache 에 보관하고 복사 (이미 있으면 다운로드 생략)
set "PLAYWRIGHT_BROWSERS_PATH=%ROOT%\packaging\.chromium-cache"
"%VENV%\Scripts\python.exe" -m playwright install --no-shell chromium || goto :fail
set "PLAYWRIGHT_BROWSERS_PATH="
robocopy "%ROOT%\packaging\.chromium-cache" "%ROOT%\dist\NaverBlogStudio\ms-playwright" /E /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 goto :fail

echo [4/4] 설치 프로그램 생성...
if not exist "%ROOT%\packaging\MicrosoftEdgeWebview2Setup.exe" (
  echo     WebView2 런타임 설치기 다운로드...
  curl.exe -L -s -o "%ROOT%\packaging\MicrosoftEdgeWebview2Setup.exe" "https://go.microsoft.com/fwlink/p/?LinkId=2124703" || goto :fail
)
set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
  echo [오류] Inno Setup 6이 없습니다. winget install JRSoftware.InnoSetup 으로 설치하세요.
  goto :fail
)
"%ISCC%" /DAppVersion=%APPVER% packaging\installer.iss || goto :fail

echo.
echo 완료: packaging\output 폴더의 NaverBlogStudio-Setup-*.exe 를 배포하세요.
exit /b 0

:fail
echo.
echo [실패] 빌드 중 오류가 발생했습니다.
exit /b 1
