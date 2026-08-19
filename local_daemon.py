#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地常驻定时: 程序内循环, 按计划运行 monitor.py (本地模式 Ollama qwen2.5:7b)

优先级: 本地 > 云端 Actions
- 本地在云端计划时间前 1 分钟运行 (默认 14:39 / 19:59)
- 本地真正开始运行时, 先写带时间戳的认领标志 LOCAL_DAEMON_CLAIMED_AT
- 云端看到该标志新鲜(<=30分钟) -> 跳过, 由本地处理
- 本地运行完会清掉认领标志; 若本地崩溃, 标志会在 30 分钟后自动过期, 云端照常接管
- 本地也检查云端今日报告/正在运行, 避免重复

黑框操作:
  R = 立即运行一次
  Q = 退出
"""
import argparse
import datetime
import os
import re
import shutil
import subprocess
import sys
import time

import requests

WORKDIR = os.path.dirname(os.path.abspath(__file__))
CN_TZ = datetime.timezone(datetime.timedelta(hours=8))
UTC = datetime.timezone.utc
REPO = "menghuanshiguang/lidaxiao-monitor"
WORKFLOW_PATH = ".github/workflows/monitor.yml"
DEFAULT_TIMES = ["14:39", "19:59"]   # 比云端 14:40 / 20:00 早 1 分钟
LOG_PATH = os.path.join(WORKDIR, "data", "local_daemon.log")

try:
    import msvcrt
    HAS_MSVCRT = True
except ImportError:
    HAS_MSVCRT = False


def check_key():
    """读取控制台按键(小写); 无按键返回 None"""
    if not HAS_MSVCRT:
        return None
    try:
        if msvcrt.kbhit():
            return msvcrt.getwch().lower()
    except Exception:
        pass
    return None


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


def set_local_claimed():
    """本地开始运行时写认领标志(带时间戳), 云端据此跳过"""
    ts = datetime.datetime.now(UTC).isoformat(timespec="seconds")
    cmd = ["gh", "variable", "set", "LOCAL_DAEMON_CLAIMED_AT", "--repo", REPO, "--body", ts]
    log(f"命令: {' '.join(cmd)}")
    try:
        r = subprocess.run(cmd, check=False, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30)
        log(f"返回码={r.returncode} stdout={r.stdout.strip()!r} stderr={r.stderr.strip()!r}")
        if r.returncode != 0:
            log("⚠️ 写入认领标志失败")
        else:
            log(f"已认领本地运行: claimed_at={ts}")
    except Exception as e:
        log(f"⚠️ 写入认领标志异常: {e}")


def clear_local_claimed():
    """本地运行完/退出时清掉认领标志"""
    cmd = ["gh", "variable", "set", "LOCAL_DAEMON_CLAIMED_AT", "--repo", REPO, "--body", ""]
    log(f"命令: {' '.join(cmd)}")
    try:
        r = subprocess.run(cmd, check=False, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30)
        log(f"返回码={r.returncode} stdout={r.stdout.strip()!r} stderr={r.stderr.strip()!r}")
        if r.returncode != 0:
            log("⚠️ 清除认领标志失败")
        else:
            log("已清除本地认领标志")
    except Exception as e:
        log(f"⚠️ 清除认领标志异常: {e}")


def cloud_report_exists(today):
    url = f"https://raw.githubusercontent.com/{REPO}/main/docs/reports/{today}.md"
    log(f"检查云端报告: GET {url}")
    try:
        r = requests.get(url, timeout=15)
        log(f"  -> HTTP {r.status_code}, bytes={len(r.content)}")
        return r.status_code == 200
    except Exception as e:
        log(f"  -> 异常: {e}")
        return False


def cloud_run_in_progress(hours=3):
    api_url = f"https://api.github.com/repos/{REPO}/actions/runs"
    log(f"检查云端运行: GET {api_url}?event=schedule&per_page=10")
    try:
        r = requests.get(api_url, params={"event": "schedule", "per_page": 10}, timeout=15)
        log(f"  -> HTTP {r.status_code}")
        r.raise_for_status()
        data = r.json().get("workflow_runs", [])
        log(f"  -> 返回 workflow_runs 数量={len(data)}")
        cutoff = datetime.datetime.now(UTC) - datetime.timedelta(hours=hours)
        for run in data:
            log(f"  run id={run.get('id')} path={run.get('path')} status={run.get('status')} "
                f"conclusion={run.get('conclusion')} created={run.get('created_at')}")
        for run in data:
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
                    log("  -> 判定: 云端有进行中的匹配 run")
                    return True
        log("  -> 判定: 云端无进行中的匹配 run")
    except Exception as e:
        log(f"  -> 异常: {e}")
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
        r = subprocess.run(cmd, cwd=WORKDIR, env=env, check=False)
        log(f"  -> monitor 退出码={r.returncode}")
    except Exception as e:
        log(f"❌ 本地 monitor 运行异常: {e}")


def publish_reports(args):
    """把本地生成的 reports/ 自动提交并推送到 GitHub docs/reports"""
    reports_dir = os.path.join(WORKDIR, "reports")
    docs_dir = os.path.join(WORKDIR, "docs", "reports")
    if not os.path.isdir(reports_dir):
        log("没有 reports/ 目录, 跳过发布")
        return
    daily = sorted(f for f in os.listdir(reports_dir)
                   if f.endswith(".md") and re.match(r"^\d{4}-\d{2}-\d{2}\.md$", f))
    if not daily:
        log("没有日报文件, 跳过发布")
        return

    os.makedirs(docs_dir, exist_ok=True)
    for f in daily:
        shutil.copy2(os.path.join(reports_dir, f), os.path.join(docs_dir, f))
    # latest.md = 最新日报
    latest = daily[-1]
    shutil.copy2(os.path.join(docs_dir, latest), os.path.join(docs_dir, "latest.md"))
    # 重建 index.md
    index_daily = sorted(f for f in os.listdir(docs_dir)
                         if re.match(r"^\d{4}-\d{2}-\d{2}\.md$", f))
    links = "\n".join(f"- [{f[:-3]}](./{f})" for f in index_daily)
    with open(os.path.join(docs_dir, "index.md"), "w", encoding="utf-8") as f:
        f.write("# 📺 李大霄视频日报索引\n\n" + links + "\n")

    log("复制报告到 docs/reports 完成, 准备 git 提交推送")
    token = subprocess.run(["gh", "auth", "token"], capture_output=True,
                           text=True, timeout=30).stdout.strip()
    push_url = f"https://oauth2:{token}@github.com/{REPO}.git" if token else f"https://github.com/{REPO}.git"
    try:
        subprocess.run(["git", "add", "docs/reports"], cwd=WORKDIR,
                       check=True, capture_output=True, timeout=30)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=WORKDIR,
                              capture_output=True, timeout=30)
        if diff.returncode == 0:
            log("报告无变化, 跳过提交")
            return
        subprocess.run(["git", "commit", "-m", "docs: update local reports [skip ci]"],
                       cwd=WORKDIR, check=True, capture_output=True, timeout=30)
        subprocess.run(["git", "push", push_url, "main"],
                       cwd=WORKDIR, check=True, capture_output=True, timeout=120)
        log("✅ 报告已提交并推送")
    except Exception as e:
        log(f"⚠️ 报告发布失败: {e}")


def run_slot(args):
    today = today_str()
    yesterday = (datetime.datetime.now(CN_TZ).date() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    log(f"[slot] 开始检查 (yesterday={yesterday}, today={today})")
    y_exists = cloud_report_exists(yesterday)
    t_exists = cloud_report_exists(today)
    if y_exists and t_exists:
        log("✅ 云端已有昨天和今天的报告, 本地跳过")
        return
    if y_exists:
        log("ℹ️ 云端已有昨天报告")
    else:
        log("ℹ️ 云端没有昨天报告, 本地需要补")
    if t_exists:
        log("ℹ️ 云端已有今天报告")
    else:
        log("ℹ️ 云端没有今天报告, 本地需要补")
    if cloud_run_in_progress():
        log("☁️ 云端任务进行中, 本地跳过")
        return
    log("🚀 云端未处理, 本地开始运行 (Ollama qwen2.5:7b)")
    set_local_claimed()
    try:
        run_monitor(args)
        publish_reports(args)
    finally:
        clear_local_claimed()


def main():
    ap = argparse.ArgumentParser(description="本地常驻定时 (程序内循环, 本地优先)")
    ap.add_argument("--once", action="store_true", help="立即执行一次后退出")
    ap.add_argument("--times", default=",".join(DEFAULT_TIMES),
                    help="运行时间(北京时间, 逗号分隔), 默认 14:39,19:59 (比云端早1分钟)")
    ap.add_argument("--grace-minutes", type=int, default=0,
                    help="兼容参数(已不再等待, 保留无效果)")
    ap.add_argument("--config", default=None, help="传给 monitor.py 的配置文件")
    args = ap.parse_args()

    times = [t.strip() for t in args.times.split(",") if t.strip()]
    log(f"本地常驻启动: 计划 {times} (北京时间), 云端仓库 {REPO} (黑框内按 R 立即运行 / Q 退出)")

    if args.once:
        run_slot(args)
        return 0

    last_logged_next = None
    try:
        while True:
            try:
                now = datetime.datetime.now(CN_TZ)
                nxt = next_run(now, times)
                if now >= nxt:
                    run_slot(args)
                    last_logged_next = None
                    continue
                if nxt != last_logged_next:
                    wait_min = (nxt - now).total_seconds() / 60.0
                    log(f"下一次运行: {nxt.strftime('%Y-%m-%d %H:%M:%S')} (等待 {wait_min:.1f} 分钟, 按 R 立即运行 / Q 退出)")
                    last_logged_next = nxt
                # 等待 30 秒, 期间可按键: R=立即运行, Q=退出
                for _ in range(30):
                    time.sleep(1)
                    key = check_key()
                    if key == "r":
                        log("🔄 手动触发立即运行")
                        run_slot(args)
                        last_logged_next = None
                        break
                    if key == "q":
                        log("🛑 手动退出")
                        raise KeyboardInterrupt
            except KeyboardInterrupt:
                raise
            except Exception as e:
                # 常驻化: 任何异常只记日志, 继续等待下一次触发, 不让进程退出
                log(f"❌ 守护循环异常(已忽略, 继续运行): {e}")
                time.sleep(30)
    except KeyboardInterrupt:
        log("收到退出信号")
    finally:
        clear_local_claimed()


if __name__ == "__main__":
    sys.exit(main())
