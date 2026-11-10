@echo off
rem Harness I/O Monitor - standalone desktop launcher.
rem Uses pythonw so no console window appears.
rem Errors are captured to run.log.

setlocal
cd /d "%~dp0"

set "PYW="
for %%P in (pythonw.exe) do if not defined PYW set "PYW=%%~$PATH:P"

if not defined PYW (
    echo pythonw.exe not found in PATH.
    echo Falling back to python.exe - a console window will stay open.
    python "%~dp0desktop.py"
    if errorlevel 1 pause
    exit /b
)

start "" "%PYW%" "%~dp0desktop.py"
exit /b 0
