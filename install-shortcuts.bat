@echo off
rem ---------------------------------------------------------------------
rem  Creates Start Menu and Desktop shortcuts with the app icon, so
rem  Transcriptor Prime can be pinned to the Windows taskbar.
rem
rem  Run run.bat at least once first, so the environment exists.
rem ---------------------------------------------------------------------
setlocal
cd /d "%~dp0"

where pwsh >nul 2>nul
if errorlevel 1 (
    powershell -NoProfile -ExecutionPolicy Bypass -File "tools\install_shortcuts.ps1" %*
) else (
    pwsh -NoProfile -ExecutionPolicy Bypass -File "tools\install_shortcuts.ps1" %*
)

echo.
pause
endlocal
