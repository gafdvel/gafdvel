@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo [1/3] PyInstaller...
pyinstaller --noconfirm gafdvel.spec
if errorlevel 1 exit /b 1

echo [2/3] Copy exe...
copy /Y "dist\gafdvel.exe" "gafdvel.exe" >nul

echo [3/3] Clean build junk...
if exist build rd /s /q build
if exist dist rd /s /q dist
if exist __pycache__ rd /s /q __pycache__

echo OK: gafdvel.exe
dir gafdvel.exe | findstr gafdvel.exe
