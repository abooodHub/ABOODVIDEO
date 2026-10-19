@echo off
setlocal enabledelayedexpansion
title ABOODVIDEO Studio v1.0.0

echo ========================================================
echo   ABOODVIDEO Studio v1.0.0 - Desktop Launcher
echo ========================================================

set "SCRIPT_DIR=%~dp0"
set "REPO_VENV=%SCRIPT_DIR%.venv\Scripts\python.exe"

if exist "%REPO_VENV%" (
    echo [OK] Using repository virtual environment: .venv
    set "PYTHON_EXEC=%REPO_VENV%"
    goto :RUN_STUDIO
)

where py.exe >nul 2>&1
if %ERRORLEVEL% equ 0 (
    echo [INFO] Searching for Python 3.12 runtime...
    py.exe -3.12 -c "import sys" >nul 2>&1
    if !ERRORLEVEL! equ 0 (
        echo [OK] Using Python 3.12 via py launcher
        set "PYTHON_EXEC=py.exe -3.12"
        goto :RUN_STUDIO
    )
)

where python.exe >nul 2>&1
if %ERRORLEVEL% equ 0 (
    echo [OK] Using system python.exe
    set "PYTHON_EXEC=python.exe"
    goto :RUN_STUDIO
)

echo.
echo ========================================================
echo [ERROR] No permanent Python runtime detected!
echo.
echo ABOODVIDEO Studio requires a permanent Python environment.
echo To set up the repository-local environment, run:
echo.
echo   python -m venv .venv
echo   .\.venv\Scripts\pip install -r requirements-studio.txt
echo.
echo ========================================================
pause
exit /b 1

:RUN_STUDIO
"%PYTHON_EXEC%" -m studio.main %*
if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERROR] ABOODVIDEO Studio exited with error code %ERRORLEVEL%.
    pause
    exit /b %ERRORLEVEL%
)
