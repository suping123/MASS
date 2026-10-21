@echo off
rem ---------------------------------------------------------------
rem  Double-click this file to open a shell with .venv activated.
rem  (cmd users can also run:  .venv\Scripts\activate )
rem ---------------------------------------------------------------
cd /d "%~dp0"

if not exist ".venv\Scripts\activate.bat" (
    echo [ERROR] .venv not found in: %cd%
    echo.
    echo Create it first:
    echo     python -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

echo Activating virtual environment: %cd%\.venv
cmd /k ".venv\Scripts\activate.bat"
