@echo off
rem ---------------------------------------------------------------------
rem  Transcriptor Prime launcher.
rem
rem  First run: creates a .venv next to this file and installs dependencies
rem  (~150 MB download). Every run after that starts the app directly.
rem ---------------------------------------------------------------------
setlocal
cd /d "%~dp0"

set "PYTHON=.venv\Scripts\python.exe"

if not exist "%PYTHON%" (
    echo First run - setting up a private Python environment.
    echo This downloads about 150 MB and takes a few minutes.
    echo.

    where py >nul 2>nul
    if errorlevel 1 (
        python -m venv .venv
    ) else (
        py -3 -m venv .venv
    )
    if errorlevel 1 (
        echo.
        echo Could not create the virtual environment.
        echo Install Python 3.10 or newer from https://python.org and try again.
        pause
        exit /b 1
    )

    "%PYTHON%" -m pip install --upgrade pip
    "%PYTHON%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo Dependency installation failed. See the messages above.
        pause
        exit /b 1
    )
    echo.
    echo Setup complete.
    echo.
)

rem  An existing .venv predates whatever requirements.txt asks for today, and
rem  the block above only ever runs once. Probe the imports the app cannot
rem  start without and top the environment up if any of them are missing.
"%PYTHON%" -c "import customtkinter, faster_whisper, av" >nul 2>nul
if errorlevel 1 (
    echo Installing updated dependencies.
    echo.
    "%PYTHON%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo Dependency installation failed. See the messages above.
        pause
        exit /b 1
    )
    echo.
)

set "PYTHONPATH=%~dp0src"
start "" "%~dp0.venv\Scripts\pythonw.exe" -m transcriptor_prime

endlocal
