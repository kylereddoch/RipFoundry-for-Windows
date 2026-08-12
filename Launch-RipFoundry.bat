@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>&1
if %errorlevel%==0 (
  py -3 ripfoundry.py
) else (
  python ripfoundry.py
)
if errorlevel 1 pause
