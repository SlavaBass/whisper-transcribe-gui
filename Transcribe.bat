@echo off
REM Double-click launcher for Transcribe.pyw
REM Uses pythonw.exe so no console window appears -- the GUI shows everything,
REM and a copy of the log is written to .\logs\ for later reference.

setlocal

REM Pick up PATH changes (e.g. after installing ffmpeg with winget)
set "PATH=%PATH%;%LOCALAPPDATA%\Microsoft\WinGet\Links"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"

set "GUI=%~dp0Transcribe.pyw"

if not exist "%GUI%" (
    echo Cannot find "%GUI%".
    echo.
    pause
    exit /b 1
)

where pythonw.exe >nul 2>nul
if %ERRORLEVEL%==0 (
    start "" pythonw.exe "%GUI%"
    exit /b 0
)

where python.exe >nul 2>nul
if %ERRORLEVEL%==0 (
    start "" python.exe "%GUI%"
    exit /b 0
)

echo Python was not found on PATH.
echo Install it from https://python.org and tick "Add python.exe to PATH".
echo.
pause
exit /b 1
