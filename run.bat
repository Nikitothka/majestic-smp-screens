@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m medreport.gui
) else (
  python -m medreport.gui
)
if errorlevel 1 pause
