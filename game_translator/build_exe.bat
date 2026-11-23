@echo off
rem Builds GameTranslator.exe (no Python needed to run it afterwards).
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    py -3.12 -m venv .venv || python -m venv .venv
)

echo Installing packages...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt pyinstaller || goto :error

echo Building GameTranslator.exe ...
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --windowed --name GameTranslator ^
    --paths . --collect-submodules app --collect-all rapidocr --collect-all onnxruntime ^
    main.py || goto :error

echo.
echo Done. Your program is in:  dist\GameTranslator\GameTranslator.exe
echo Copy the whole dist\GameTranslator folder anywhere, and make a desktop shortcut to the .exe.
pause
exit /b 0

:error
echo.
echo Something went wrong. Take a photo of this window and send it.
pause
exit /b 1
