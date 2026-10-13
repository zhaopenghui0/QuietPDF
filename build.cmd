@echo off
chcp 65001 >nul
cd /d "%~dp0"
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --windowed --onedir --distpath build\release --name QingYue --collect-all tkinterdnd2 --exclude-module numpy --exclude-module matplotlib --exclude-module pandas reader.py
if errorlevel 1 exit /b 1
xcopy /E /I /Y "build\release\QingYue\_internal" "dist\QingYue\_internal" >nul
if errorlevel 1 exit /b 1
copy /y "build\release\QingYue\QingYue.exe" "dist\QingYue\QingYue.exe" >nul
if errorlevel 1 exit /b 1
copy /y "README.md" "dist\QingYue\README.md" >nul
copy /y "使用示例.pdf" "dist\QingYue\使用示例.pdf" >nul
