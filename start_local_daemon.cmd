@echo off
rem 启动本地常驻监控 (Ollama qwen2.5:7b, 本地优先, 与云端 Actions 防冲突)
cd /d %~dp0
if not exist data mkdir data
start "LiDaxiao Local Monitor" /min pythonw local_daemon.py
echo 本地监控已后台启动 (日志: data\local_daemon.log)
