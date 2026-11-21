@echo off
rem Kmong edition setup: same dist build + edition.txt (search data features off).
rem Run build_installer.bat first (dist\NaverBlogStudio must hold the current code).
setlocal
cd /d "%~dp0.."
set /p APPVER=<packaging\appver.txt
if "%APPVER%"=="" goto :fail
set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
"%ISCC%" /DAppVersion=%APPVER% /DEdition=kmong packaging\installer.iss || goto :fail
echo Done: packaging\output\NaverBlogStudio-Setup-%APPVER%-kmong.exe
exit /b 0
:fail
echo [FAILED]
exit /b 1
