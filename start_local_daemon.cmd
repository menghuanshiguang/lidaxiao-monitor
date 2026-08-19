@echo off
rem Start local monitor daemon (Ollama qwen2.5:7b, local-first, cloud conflict-free)
cd /d %~dp0
if not exist data mkdir data
python local_daemon.py
echo.
echo Local monitor exited (errorlevel %errorlevel%)
pause
