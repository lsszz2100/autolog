@echo off
rem 설치 프로그램 빌드는 packaging\build_installer.bat 사용 (Chromium 포함 + Inno Setup 설치 파일 생성)
call "%~dp0packaging\build_installer.bat"
pause
