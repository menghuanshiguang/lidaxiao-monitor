@echo off
rem ============================================================
rem LiDaxiao monitor - run wrapper (manual & scheduled use)
rem Usage: run_monitor.cmd [--force]
rem Log:   data\run.log (appended)
rem ============================================================
setlocal
cd /d "%~dp0"

rem Fixed HOME so bilidown finds login cookies
set "HOME=%USERPROFILE%"

rem Add ffmpeg / python toolchain to PATH
set "TOOLS=%CD%\tools\ffmpeg\bin"
if exist "%TOOLS%\ffmpeg.exe" set "PATH=%TOOLS%;%PATH%"
set "PATH=%APPDATA%\Python\Python312\Scripts;%PATH%"

set "PYTHONIOENCODING=utf-8"

echo [%date% %time%] ====== monitor run ====== >> data\run.log
python monitor.py %* >> data\run.log 2>&1
set "RC=%ERRORLEVEL%"
echo [%date% %time%] exit=%RC% >> data\run.log
exit /b %RC%
