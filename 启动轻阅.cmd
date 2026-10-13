@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "dist\QingYue\QingYue-黑板擦.exe" (
    start "" "dist\QingYue\QingYue-黑板擦.exe" %*
) else if exist "dist\QingYue\QingYue.exe" (
    start "" "dist\QingYue\QingYue.exe" %*
) else (
    start "" ".venv\Scripts\pythonw.exe" "reader.py" %*
)
