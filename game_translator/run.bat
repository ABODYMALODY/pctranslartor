@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    py -3.12 -m venv .venv || python -m venv .venv
)

if not exist ".venv\installed.txt" (
    echo Installing packages, first run only...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt && echo ok> ".venv\installed.txt"
)

".venv\Scripts\python.exe" main.py
if errorlevel 1 pause
