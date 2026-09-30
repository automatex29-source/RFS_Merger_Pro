@echo off
setlocal
title RFS Merger Pro
cd /d "%~dp0"
echo.
echo  ==============================================
echo    RFS MERGER PRO - one-click setup and start
echo  ==============================================
echo.

REM ---- 1. Find Python (installs it automatically if missing) ----
set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY python --version >nul 2>&1 && set "PY=python"
if not defined PY goto :getpython
goto :havepython

:getpython
echo [..] Python not found. Trying to install it automatically...
winget --version >nul 2>&1
if errorlevel 1 goto :manualpython
winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
echo.
echo [OK] Python installed. Please CLOSE this window and double-click START.bat again.
pause
exit /b 0

:manualpython
echo [!!] Could not install Python automatically.
echo      Opening the download page. Install it and tick "Add Python to PATH",
echo      then double-click START.bat again.
start "" https://www.python.org/downloads/
pause
exit /b 1

:havepython
echo [OK] Python found

REM ---- 2. Create virtual environment (first run only) ----
if exist ".venv\Scripts\python.exe" goto :venvok
echo [..] Creating virtual environment...
%PY% -m venv .venv
if errorlevel 1 goto :fail
:venvok
echo [OK] Virtual environment ready

REM ---- 3. Install / update packages (skipped when nothing changed) ----
fc /b requirements.txt .venv\installed.txt >nul 2>&1
if not errorlevel 1 goto :depsok
echo [..] Downloading and installing required packages. This can take a few minutes the first time...
".venv\Scripts\python.exe" -m pip install --upgrade pip --no-cache-dir
".venv\Scripts\python.exe" -m pip install -r requirements.txt --no-cache-dir
if errorlevel 1 goto :fail
copy /y requirements.txt .venv\installed.txt >nul
:depsok
echo [OK] All packages installed

REM ---- 4. Start the app (opens your browser automatically) ----
echo.
echo [..] Starting RFS Merger Pro. Your browser will open shortly.
echo      Keep this window open while using the app. Close it to stop.
echo.
".venv\Scripts\python.exe" rfs_merger_backend.py
pause
exit /b 0

:fail
echo.
echo [ERROR] Setup failed.
echo         - If you saw "No space left on device": free up disk space
echo           (a couple of GB is enough), delete the ".venv" folder, and
echo           run START.bat again.
echo         - Otherwise: check your internet connection, delete ".venv",
echo           and run START.bat again.
pause
exit /b 1
