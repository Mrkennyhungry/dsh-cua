@echo off
rem Double-click entry point: publishes to npm and cuts the GitHub Release.
rem Both tokens are asked for at a hidden prompt and never written to the repo.
setlocal
cd /d "%~dp0"
where pwsh >nul 2>nul
if errorlevel 1 (
  echo PowerShell 7 ^(pwsh^) was not found. Install it, or run:
  echo   powershell -ExecutionPolicy Bypass -File "%~dp0scripts\publish.ps1"
  pause
  exit /b 1
)
pwsh -NoProfile -File "%~dp0scripts\publish.ps1" %*
echo.
pause
