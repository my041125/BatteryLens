@echo off
rem Keep this file ASCII-only: cmd parses .bat with the OEM codepage, and
rem non-ASCII comments can decode into quote/paren bytes that shred blocks.
rem Delayed expansion is required: %VAR% inside a ( ... ) block expands at
rem parse time (to empty here), which broke the dependency check below.
setlocal EnableDelayedExpansion
title BatteryScope
cd /d "%~dp0"

rem ===== 1. Prefer a Python with full deps (winrt / hidapi / pywin32) =====
rem The WorkBuddy bundled Python ships them; a system Python often lacks
rem winrt / hid, which shows the window but reads zero devices.
set "PY="
set "PW="
set "WBROOT=%USERPROFILE%\.workbuddy\binaries\python\versions"
if exist "!WBROOT!\" (
  for /f "delims=" %%I in ('dir /b /ad "!WBROOT!" 2^>nul') do (
    if not defined PY if exist "!WBROOT!\%%I\python.exe" set "PY=!WBROOT!\%%I\python.exe"
    if not defined PW if exist "!WBROOT!\%%I\pythonw.exe" set "PW=!WBROOT!\%%I\pythonw.exe"
  )
  if defined PY "!PY!" -c "import winrt.windows.devices.bluetooth,hid,win32gui" >nul 2>nul
  if errorlevel 1 (set "PY=" & set "PW=")
)

rem ===== 2. Fall back to the Python on PATH =====
if not defined PY (
  where python >nul 2>nul && set "PY=python"
  if not defined PY (where python3 >nul 2>nul && set "PY=python3")
  if defined PY "!PY!" -c "import winrt.windows.devices.bluetooth,hid,win32gui" >nul 2>nul
  if errorlevel 1 echo [warn] this Python lacks winrt / hidapi; device battery may be unreadable.
  if defined PY for /f "delims=" %%I in ('where !PY! 2^>nul') do if not defined PW (
    for %%J in ("%%~fI") do if exist "%%~dpJpythonw.exe" set "PW=%%~dpJpythonw.exe"
  )
)

if not defined PY (
  echo Python not found. Please install Python 3.10+ and run this file again.
  pause
  exit /b 1
)

rem Prefer pythonw.exe: no console window, no focus steal on start.
echo BatteryScope starting. Check the small card at the bottom-right of the desktop.
if defined PW (
  start "" "!PW!" widget.py
) else (
  start "" "!PY!" widget.py
)
