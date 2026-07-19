@echo off
REM ============================================================
REM run_all.bat
REM Launch the Invisible Interview Assistant (backend + overlay).
REM
REM Usage (from the project root in cmd.exe):
REM     run_all.bat
REM ============================================================

setlocal
cd /d "%~dp0"

set "PY=python"
set "VENV_PY=%~dp0.venv\Scripts\python.exe"

REM --- 1. Prefer the venv's python if it exists -----------------------
if exist "%VENV_PY%" (
    set "PY=%VENV_PY%"
    echo [run_all] using venv python: %VENV_PY%
) else (
    echo [run_all] WARNING: no .venv found at "%VENV_PY%"
    echo [run_all]          using system python instead.
)

REM --- 2. Sanity-check dependencies ------------------------------------
echo [run_all] checking dependencies...
"%PY%" -c "import fastapi, uvicorn, PyQt5, sounddevice, vosk, requests, websocket" 1>nul 2>nul
if errorlevel 1 (
    echo [run_all] FAILED: missing Python dependencies.
    echo [run_all]   Run this once first:
    echo [run_all]       pip install -r requirements.txt
    echo.
    pause
    exit /b 2
)

REM --- 3. Create logs/ directory ---------------------------------------
if not exist logs mkdir logs

REM --- 4. Track child PIDs so Ctrl+C can kill them ---------------------
set "BACKEND_PID="
set "OVERLAY_PID="

REM --- 5. Launch backend in a new console window -----------------------
echo [run_all] starting backend...
start "Interview Assistant - Backend" cmd /k "cd /d %~dp0 && "%PY%" -m backend.main"
echo [run_all] backend launched in a new window.

REM Give the backend a moment to bind the port.
timeout /t 2 /nobreak >nul

REM --- 6. Launch overlay in a new console window -----------------------
echo [run_all] starting overlay...
start "Interview Assistant - Overlay" cmd /k "cd /d %~dp0 && "%PY%" -m frontend.overlay"
echo [run_all] overlay launched in a new window.

echo.
echo ============================================================
echo Both processes are running in their own console windows.
echo - Look for "Interview Assistant - Overlay"  -^>  the GUI window
echo - Look for "Interview Assistant - Backend"  -^>  the API server
echo.
echo To stop: close the Backend and Overlay windows,
echo          or press any key here.
echo ============================================================
pause >nul

REM --- 7. On any keypress, kill our children --------------------------
echo.
echo [run_all] stopping child processes...

REM Kill anything we started that's still running.
taskkill /FI "WINDOWTITLE eq Interview Assistant - Backend*"  /T /F 1>nul 2>nul
taskkill /FI "WINDOWTITLE eq Interview Assistant - Overlay*"  /T /F 1>nul 2>nul

echo [run_all] done.
endlocal
