@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python was not found. Install Python 3.8+ and add it to PATH.
  pause
  exit /b 1
)
python "%~dp0autodl_monitor.py" %*
set "monitor_exit=%errorlevel%"
echo.
pause
exit /b %monitor_exit%
