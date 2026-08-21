@echo off
rem Start local monitor daemon (Ollama qwen2.5:7b, local-first, cloud conflict-free)
cd /d %~dp0
rem Ollama MoE: offload 40 experts to CPU (equivalent to --n-cpu-moe 40)
set OLLAMA_N_CPU_MOE=40
if not exist data mkdir data
python local_daemon.py
echo.
echo Local monitor exited (errorlevel %errorlevel%)
pause
