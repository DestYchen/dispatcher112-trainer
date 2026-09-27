@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo First install the simulator as described in README.md.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -X utf8 scripts\present.py
if errorlevel 1 pause
