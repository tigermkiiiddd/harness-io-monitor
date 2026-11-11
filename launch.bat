@echo off
rem Harness I/O Monitor - standalone desktop launcher.
rem Runs launcher.pyw with pythonw so no console window appears.
rem launcher.pyw: activates a running instance, or runs the on-demand
rem scheduled task (no UAC), or falls back to app.py (one UAC prompt).

setlocal
cd /d "%~dp0"

set "PYW="
for %%P in (pythonw.exe) do if not defined PYW set "PYW=%%~$PATH:P"

if not defined PYW (
    echo pythonw.exe not found in PATH.
    echo Falling back to python.exe - a console window will stay open.
    python "%~dp0launcher.pyw"
    if errorlevel 1 pause
    exit /b
)

start "" "%PYW%" "%~dp0launcher.pyw"
exit /b 0
