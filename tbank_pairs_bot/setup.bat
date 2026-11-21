@echo off
cd /d "%~dp0"
if not exist .venv python -m venv .venv || exit /b 1
.venv\Scripts\python -m pip install --upgrade pip || exit /b 1
.venv\Scripts\python -m pip install -r requirements.txt pytest || exit /b 1
echo.
echo Done. Try: run.bat scan
