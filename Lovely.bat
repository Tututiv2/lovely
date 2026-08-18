@echo off
setlocal
title Lovely

rem Double-click this. Nothing to install -- the launcher is standard-library Python, so
rem there is no pip install, no virtualenv, and nothing that can rot between sessions.
rem
rem Two details that matter:
rem   * Every path is quoted and %~dp0 keeps its trailing backslash, so this still works
rem     when the folder it lives in has a space in its name.
rem   * pyw / pythonw are the *windowed* interpreters: they open no console at all, so the
rem     black box does not sit behind the launcher for the whole session. `start ""` then
rem     lets this script exit immediately.
rem
rem Pass --dev to enable the local dev account (for servers with online-mode=false).

set "HERE=%~dp0"
set "APP=%HERE%run_ui.py"

where pyw >nul 2>&1 && (
    start "" pyw -3 "%APP%" %*
    goto :eof
)

where pythonw >nul 2>&1 && (
    start "" pythonw "%APP%" %*
    goto :eof
)

rem No windowed interpreter: fall back to the console one and let it keep its window.
where py >nul 2>&1 && (
    start "" py -3 "%APP%" %*
    goto :eof
)

where python >nul 2>&1 && (
    start "" python "%APP%" %*
    goto :eof
)

echo.
echo   Python was not found on PATH.
echo.
echo   Install Python 3.10 or newer from https://python.org and tick
echo   "Add python.exe to PATH" in the installer, then run this again.
echo.
pause
