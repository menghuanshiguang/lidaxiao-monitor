#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地常驻定时: 程序内循环, 按计划运行 monitor.py (本地模式 Ollama qwen2.5:7b)

优先级: 本地 > 云端 Actions
- 本地活跃时通过 GitHub repo variable 标记 (LOCAL_DAEMON_ACTIVE / LOCAL_DAEMON_UPDATED_AT)
- 云端 Actions 检测到本地活跃且未过期 -> 跳过, 由本地处理
- 本地检测到云端已完成今日报告/正在处理 -> 本地跳过, 避免重复

用法:
  python local_daemon.py            # 前台常驻
  python local_daemon.py --once     # 立即执行一次后退出
  python local_daemon.py --times 14:40,20:00 --grace-minutes 10
"""
import argparse
import datetime
import os
import subprocess
import sys
import time

import requests

WORKDIR = os.path.dirname(os.path.abspath(__file__))
CN_TZ = datetime.timezone(datetime.timedelta(hours=8))
UTC = datetime.timezone.utc
REPO = "menghuanshiguang/lidaxiao-monitor"
WORKFLOW_PATH = ".github/workflows/monitor.yml"
DEFAULT_TIMES = ["14:40", "20:00"]
HEARTBEAT_SECONDS = 300          # 每 5 分钟刷新一次本地活跃标志
LOG_PATH = os.path.join(WORKDIR, "data", "local_daemon.log")


def log(msg):
    line = f"[{datetime.datetime.now(CN_TZ).strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def parse_hhmm(s):
    h, m = s.split(":")
    return int(h), int(m)


def next_run(now, times):
    today_times = [now.replace(hour=parse_hhmm(t)[0], minute=parse_hhmm(t)[1],
                               second=0, microsecond=0) for t in times]
    for t in sorted(today_times):
        if now < t:
            return t
    h, m = parse_hhmm(times[0])
    tomorrow = now + datetime.timedelta(days=1)
    return tomorrow.replace(hour=h, minute=m, second=0, microsecond=0)


def today_str():
    return datetime.datetime.now(CN_TZ).strftime("%Y-%m-%d")


def update_local_flag(active):
    """通过 GitHub repo variable 标记本地守护是否活跃 (云端据此决定是否跳过)"""
    val = "true" if active else "false"
    ts = datetime.datetime.now(UTC).isoformat(timespec="seconds")
    try:
        subprocess.run(["gh", "variable", "set", "LOCAL_DAEMON_ACTIVE",
                        "--repo", REPO, "--body", val],
                       check=True, capture_output=True, timeout=30)
        subprocess.run(["gh", "variable", "set", "LOCAL_DAEMON_UPDATED_AT",
                        "--repo", REPO, "--body", ts],
                       check=True, capture_output=True, timeout=30)
        log(f"本地标志已更新: active={val} updated_at={ts}")
    except Exception as e:
        log(f"⚠️ 更新本地标志失败: {e}")


def cloud_report_exists(today):
    url = f"https://raw.githubusercontent.com/{REPO}/main/docs/reports/{today}.md"
    try:
        r = requests.get(url, timeout=15)
        return r.status_code == 200
    except Exception as e:
        log(f"⚠️ 查询云端报告失败: {e}")
        return False


def cloud_run_in_progress(hours=3):
    try:
        r = requests.get(f"https://api.github.com/repos/{REPO}/actions/runs",
                         params={"event": "schedule", "per_page": 10}, timeout=15)
        r.raise_for_status()
        cutoff = datetime.datetime.now(UTC) - datetime.timedelta(hours=hours)
        for run in r.json().get("workflow_runs", []):
            if run.get("path") != WORKFLOW_PATH:
                continue
            status = run.get("status")
            created = run.get("created_at")
            if created and status in ("queued", "in_progress"):
                try:
                    created_dt = datetime.datetime.fromisoformat(created.replace("Z", "+00:00"))
                except Exception:
                    created_dt = None
                if created_dt and created_dt >= cutoff:
                    return True
    except Exception as e:
        log(f"⚠️ 查询云端运行状态失败: {e}")
    return False


def run_monitor(args):
    cmd = [sys.executable, os.path.join(WORKDIR, "monitor.py")]
    if args.config:
        cmd += ["--config", args.config]
    env = dict(os.environ)
    env["LIDAXIAO_LOCAL"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    log("执行: " + " ".join(cmd))
    try:
        subprocess.run(cmd, cwd=WORKDIR, env=env, check=False)
    except Exception as e:
        log(f"❌ 本地 monitor 运行异常: {e}")


def run_slot(args):
    today = today_str()
    log(f"[slot] 开始检查 (today={today})")
    if cloud_report_exists(today):
        log("✅ 云端已完成今日报告, 本地跳过")
        return
    if cloud_run_in_progress():
        log("☁️ 云端任务进行中, 本地跳过")
        return
    deadline = datetime.datetime.now(CN_TZ) + datetime.timedelta(minutes=args.grace_minutes)
    log(f"⏳ 未发现云端处理, 等待 {args.grace_minutes} 分钟观察云端...")
    while datetime.datetime.now(CN_TZ) < deadline:
        time.sleep(30)
        if cloud_report_exists(today):
            log("✅ 等待期间云端已出报告, 本地跳过")
            return
        if cloud_run_in_progress():
            log("☁️ 等待期间云端开始处理, 本地跳过")
            return
    log("🚀 云端未处理, 本地开始运行 (Ollama qwen2.5:7b)")
    run_monitor(args)


def main():
    ap = argparse.ArgumentParser(description="本地常驻定时 (程序内循环, 不与云端 Actions 冲突)")
    ap.add_argument("--once", action="store_true", help="立即执行一次后退出")
    ap.add_argument("--times", default=",".join(DEFAULT_TIMES),
                    help="运行时间(北京时间, 逗号分隔), 默认 14:40,20:00")
    ap.add_argument("--grace-minutes", type=int, default=10, help="云端观察宽限分钟数")
    ap.add_argument("--config", default=None, help="传给 monitor.py 的配置文件")
    args = ap.parse_args()

    times = [t.strip() for t in args.times.split(",") if t.strip()]
    log(f"本地常驻启动: 计划 {times} (北京时间), 云端仓库 {REPO}")
    update_local_flag(True)

    if args.once:
        try:
            run_slot(args)
        finally:
            update_local_flag(False)
        return 0

    last_beat = time.time()
    try:
        while True:
            try:
                now = datetime.datetime.now(CN_TZ)
                nxt = next_run(now, times)
                if now >= nxt:
                    run_slot(args)
                    continue
                time.sleep(30)
                if time.time() - last_beat >= HEARTBEAT_SECONDS:
                    update_local_flag(True)
                    last_beat = time.time()
            except KeyboardInterrupt:
                raise
            except Exception as e:
                # 常驻化: 任何异常只记日志, 继续等待下一次触发, 不让进程退出
                log(f"❌ 守护循环异常(已忽略, 继续运行): {e}")
                time.sleep(30)
    except KeyboardInterrupt:
        log("收到退出信号, 标记本地停止")
    finally:
        update_local_flag(False)


if __name__ == "__main__":
    sys.exit(main())
