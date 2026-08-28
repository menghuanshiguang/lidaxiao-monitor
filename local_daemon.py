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
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

import requests

WORKDIR = os.path.dirname(os.path.abspath(__file__))
CN_TZ = datetime.timezone(datetime.timedelta(hours=8))
UTC = datetime.timezone.utc
REPO = "menghuanshiguang/lidaxiao-monitor"
WORKFLOW_PATH = ".github/workflows/monitor.yml"
DEFAULT_TIMES = ["14:39", "19:59"]   # 比云端 14:40 / 20:00 早 1 分钟
SLOT_GRACE_MINUTES = 60              # 错过 slot 后 60 分钟内仍补跑, 避免醒来晚几秒漏触发
LOG_PATH = os.path.join(WORKDIR, "data", "local_daemon.log")

try:
    import msvcrt
    HAS_MSVCRT = True
except ImportError:
    HAS_MSVCRT = False

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    import ctypes
except ImportError:
    ctypes = None


def _disable_quickedit():
    """关闭控制台 QuickEdit, 防止鼠标点击选中文本后输出冻结(需按回车才恢复)"""
    if ctypes is None:
        return
    try:
        kernel32 = ctypes.windll.kernel32
        STD_INPUT_HANDLE = -10
        ENABLE_EXTENDED_FLAGS = 0x0080
        ENABLE_QUICK_EDIT_MODE = 0x0040
        handle = kernel32.GetStdHandle(STD_INPUT_HANDLE)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            new_mode = (mode.value & ~ENABLE_QUICK_EDIT_MODE) | ENABLE_EXTENDED_FLAGS
            kernel32.SetConsoleMode(handle, new_mode)
    except Exception:
        pass


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


def slot_times(now, times):
    return sorted(now.replace(hour=parse_hhmm(t)[0], minute=parse_hhmm(t)[1],
                              second=0, microsecond=0) for t in times)


def next_future_slot(now, times):
    """严格未来下一次运行时间(用于日志)"""
    for t in slot_times(now, times):
        if now < t:
            return t
    h, m = parse_hhmm(times[0])
    tomorrow = now + datetime.timedelta(days=1)
    return tomorrow.replace(hour=h, minute=m, second=0, microsecond=0)


def find_due_slot(now, times, last_run_slot):
    """找当前应触发的 slot: 时间已到且在宽限期内, 且不是已经跑过的 slot"""
    for t in slot_times(now, times):
        if last_run_slot and t <= last_run_slot:
            continue
        if t <= now <= t + datetime.timedelta(minutes=SLOT_GRACE_MINUTES):
            return t
    return None


def today_str():
    return datetime.datetime.now(CN_TZ).strftime("%Y-%m-%d")


def set_local_claimed():
    """本地开始运行时写认领标志(带时间戳), 云端据此跳过; 重试3次, 失败返回 False"""
    ts = datetime.datetime.now(UTC).isoformat(timespec="seconds")
    cmd = ["gh", "variable", "set", "LOCAL_DAEMON_CLAIMED_AT", "--repo", REPO, "--body", ts]
    for attempt in range(1, 4):
        log(f"命令(第{attempt}次): {' '.join(cmd)}")
        try:
            r = subprocess.run(cmd, check=False, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=30)
            log(f"返回码={r.returncode} stdout={r.stdout.strip()!r} stderr={r.stderr.strip()!r}")
            if r.returncode == 0:
                log(f"已认领本地运行: claimed_at={ts}")
                return True
            log(f"⚠️ 写入认领标志失败(第{attempt}次)")
        except Exception as e:
            log(f"⚠️ 写入认领标志异常(第{attempt}次): {e}")
        if attempt < 3:
            time.sleep(2)
    return False


def clear_local_claimed():
    """本地运行完/退出时删除认领标志 (变量删除比空值更稳)"""
    cmd = ["gh", "variable", "delete", "LOCAL_DAEMON_CLAIMED_AT", "--repo", REPO]
    log(f"命令: {' '.join(cmd)}")
    try:
        r = subprocess.run(cmd, check=False, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30)
        log(f"返回码={r.returncode} stdout={r.stdout.strip()!r} stderr={r.stderr.strip()!r}")
        if r.returncode != 0:
            log("⚠️ 删除认领标志失败(可能不存在, 可忽略)")
        else:
            log("已清除本地认领标志")
    except Exception as e:
        log(f"⚠️ 删除认领标志异常: {e}")


def claim_is_fresh(max_age_minutes=30):
    """查询 GitHub 认领标志是否新鲜(本地是否在跑), 供云端判断"""
    cmd = ["gh", "variable", "get", "LOCAL_DAEMON_CLAIMED_AT", "--repo", REPO]
    log(f"命令: {' '.join(cmd)}")
    try:
        r = subprocess.run(cmd, check=False, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30)
        if r.returncode != 0:
            log("查询认领标志: 不存在")
            return False
        val = r.stdout.strip()
        log(f"查询认领标志: {val!r}")
        if not val:
            return False
        t = datetime.datetime.fromisoformat(val.replace("Z", "+00:00"))
        age = (datetime.datetime.now(UTC) - t).total_seconds() / 60
        log(f"认领标志年龄: {age:.1f} 分钟")
        return age <= max_age_minutes
    except Exception as e:
        log(f"⚠️ 查询认领标志异常: {e}")
        return False


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
    log(f"检查云端运行: GET {api_url}?per_page=20")
    try:
        r = requests.get(api_url, params={"per_page": 20}, timeout=15)
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


def run_monitor(args, local=True):
    cmd = [sys.executable, os.path.join(WORKDIR, "monitor.py")]
    if args.config:
        cmd += ["--config", args.config]
    env = dict(os.environ)
    if local:
        env["LIDAXIAO_LOCAL"] = "1"
        env.setdefault("OLLAMA_N_CPU_MOE", "40")
    env["PYTHONIOENCODING"] = "utf-8"
    log("执行: " + " ".join(cmd))
    try:
        r = subprocess.run(cmd, cwd=WORKDIR, env=env, check=False)
        log(f"  -> monitor 退出码={r.returncode}")
    except Exception as e:
        log(f"❌ 本地 monitor 运行异常: {e}")


def _ensure_git_user():
    subprocess.run(["git", "config", "user.name", "lidaxiao-monitor[bot]"],
                   cwd=WORKDIR, capture_output=True, timeout=15)
    subprocess.run(["git", "config", "user.email", "actions@users.noreply.github.com"],
                   cwd=WORKDIR, capture_output=True, timeout=15)


def _push_url():
    token = subprocess.run(["gh", "auth", "token"], capture_output=True,
                           text=True, timeout=30).stdout.strip()
    return f"https://oauth2:{token}@github.com/{REPO}.git" if token else f"https://github.com/{REPO}.git"


def _merge_report(new_text, old_text):
    """合并两份日报: 保留旧报告中不在新报告里的视频章节, 新报告的章节覆盖旧的同BV章节"""
    # 解析旧报告的视频章节 (按BV定位)
    sec_re = re.compile(r"^## 视频\d+:《(.+?)》\s*\(?(BV\w+)\)?\s*$")
    old_secs = {}  # bvid -> (header_line, content)
    old_lines = old_text.splitlines()
    cur_bvid = None
    cur_start = None
    for i, ln in enumerate(old_lines):
        m = sec_re.match(ln)
        if m:
            if cur_bvid and cur_start is not None:
                old_secs[cur_bvid] = "\n".join(old_lines[cur_start:i])
            cur_bvid = m.group(2)
            cur_start = i
    if cur_bvid and cur_start is not None:
        old_secs[cur_bvid] = "\n".join(old_lines[cur_start:])
    if not old_secs:
        return new_text  # 旧报告无法解析, 直接用新报告
    # 解析新报告的视频章节
    new_secs = {}  # bvid -> (header_line, content)
    new_lines = new_text.splitlines()
    cur_bvid = None
    cur_start = None
    for i, ln in enumerate(new_lines):
        m = sec_re.match(ln)
        if m:
            if cur_bvid and cur_start is not None:
                new_secs[cur_bvid] = "\n".join(new_lines[cur_start:i])
            cur_bvid = m.group(2)
            cur_start = i
    if cur_bvid and cur_start is not None:
        new_secs[cur_bvid] = "\n".join(new_lines[cur_start:])
    # 合并: 新报告的章节 + 旧报告中不在新报告里的章节
    merged_secs = []
    added = set()
    for bvid, sec_text in new_secs.items():
        merged_secs.append(sec_text)
        added.add(bvid)
    for bvid, sec_text in old_secs.items():
        if bvid not in added:
            merged_secs.append(sec_text)
    # 重建报告: 取新报告的 header + summary + list, 追加合并后的章节
    sec_start = len(new_lines)
    for i, ln in enumerate(new_lines):
        if sec_re.match(ln):
            sec_start = i
            break
    header = "\n".join(new_lines[:sec_start]).rstrip()
    body = "\n\n".join(merged_secs)
    # 重新编号章节 (合并后序号可能重复)
    n_gen = (i for i in range(1, 100))
    body = re.sub(r"^## 视频\d+:", lambda m: f"## 视频{next(n_gen)}:", body, flags=re.M)
    return header + "\n\n" + body + "\n"


def _pull_rebase(push_url):
    """push 前合并远端; 冲突时用 -X ours 让本地版本优先 (本地优先)"""
    r = subprocess.run(
        ["git", "pull", "--no-rebase", "--no-edit", "-X", "ours", push_url, "main"],
        cwd=WORKDIR, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60)
    log(f"push 前合并: 返回码={r.returncode} stdout={r.stdout.strip()!r} stderr={r.stderr.strip()!r}")
    if r.returncode != 0:
        log("⚠️ 合并失败, 执行 merge --abort 恢复")
        subprocess.run(["git", "merge", "--abort"], cwd=WORKDIR,
                       capture_output=True, timeout=30)
        return False
    return True


PROTECTED_PATHS = ("docs/reports", "state")


def _snapshot_protected():
    """推送前备份本地生成的报告/状态文件, 合并后强制恢复成本地版本"""
    tmp = tempfile.mkdtemp(prefix="dsh_push_")
    for rel in PROTECTED_PATHS:
        src = os.path.join(WORKDIR, rel)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(tmp, rel))
        elif os.path.isfile(src):
            os.makedirs(os.path.dirname(os.path.join(tmp, rel)), exist_ok=True)
            shutil.copy2(src, os.path.join(tmp, rel))
    return tmp


def _restore_protected(tmp):
    """把 docs/reports 与 state 强制覆盖成本地版本, 并 git add"""
    for rel in PROTECTED_PATHS:
        src_root = os.path.join(tmp, rel)
        dst_root = os.path.join(WORKDIR, rel)
        if not os.path.exists(src_root):
            continue
        if os.path.isdir(src_root):
            for root, _, files in os.walk(src_root):
                rel_dir = os.path.relpath(root, src_root)
                dst_dir = os.path.join(dst_root, rel_dir) if rel_dir != "." else dst_root
                os.makedirs(dst_dir, exist_ok=True)
                for f in files:
                    shutil.copy2(os.path.join(root, f), os.path.join(dst_dir, f))
        elif os.path.isfile(src_root):
            os.makedirs(os.path.dirname(dst_root), exist_ok=True)
            shutil.copy2(src_root, dst_root)
        subprocess.run(["git", "add", "--", rel], cwd=WORKDIR,
                       capture_output=True, timeout=30)


def _push():
    """提交后执行 merge+push; 报告/state 始终以本地版本为准, 成功返回 True"""
    push_url = _push_url()
    backup = _snapshot_protected()
    try:
        if not _pull_rebase(push_url):
            return False
        _restore_protected(backup)
        # 合并可能把报告/state 改坏(重复/丢章节), 强制恢复本地版本
        diff = subprocess.run(["git", "diff", "--cached", "--quiet", "--",
                               "docs/reports", "state"],
                              cwd=WORKDIR, capture_output=True, timeout=30)
        if diff.returncode != 0:
            unmerged = subprocess.run(["git", "diff", "--name-only", "--diff-filter=U"],
                                      cwd=WORKDIR, capture_output=True, text=True,
                                      encoding="utf-8", errors="replace", timeout=30)
            if unmerged.stdout.strip():
                log(f"⚠️ 仍有非报告/state 冲突未解决: {unmerged.stdout.strip()}, 中止推送")
                subprocess.run(["git", "merge", "--abort"], cwd=WORKDIR,
                               capture_output=True, timeout=30)
                return False
            in_merge = os.path.exists(os.path.join(WORKDIR, ".git", "MERGE_HEAD"))
            if in_merge:
                subprocess.run(["git", "commit", "--no-edit"], cwd=WORKDIR,
                               check=True, capture_output=True, timeout=30)
            else:
                subprocess.run(["git", "commit", "--amend", "--no-edit"], cwd=WORKDIR,
                               check=True, capture_output=True, timeout=30)
            log("✅ 已强制恢复本地报告/state 版本")
        subprocess.run(["git", "push", push_url, "main"],
                       cwd=WORKDIR, check=True, capture_output=True, timeout=120)
        return True
    finally:
        shutil.rmtree(backup, ignore_errors=True)


def commit_state(args, tag="local"):
    """没有日报时也把 state 改动提交推送, 避免本地脏状态挡住下次 pull"""
    try:
        subprocess.run(["git", "add", "state"], cwd=WORKDIR, check=True,
                       capture_output=True, timeout=30)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=WORKDIR,
                              capture_output=True, timeout=30)
        if diff.returncode == 0:
            log("state 无变化, 跳过提交")
            return
        _ensure_git_user()
        subprocess.run(["git", "commit", "-m", f"chore: update state [{tag}] [skip ci]"],
                       cwd=WORKDIR, check=True, capture_output=True, timeout=30)
        if _push():
            log("✅ state 已提交并推送")
    except Exception as e:
        log(f"⚠️ state 提交失败: {e}")


def publish_reports(args, tag="local"):
    """把生成的 reports/ 自动提交并推送到 GitHub docs/reports; tag 标识 local/cloud"""
    reports_dir = os.path.join(WORKDIR, "reports")
    docs_dir = os.path.join(WORKDIR, "docs", "reports")
    if not os.path.isdir(reports_dir):
        log("没有 reports/ 目录, 跳过发布")
        return
    baseline = datetime.datetime.now(CN_TZ).date() - datetime.timedelta(days=2)

    def is_recent(name):
        try:
            d = datetime.datetime.strptime(name[:10], "%Y-%m-%d").date()
            return d >= baseline
        except ValueError:
            return False

    daily = sorted(f for f in os.listdir(reports_dir)
                   if f.endswith(".md") and re.match(r"^\d{4}-\d{2}-\d{2}\.md$", f) and is_recent(f))
    if not daily:
        log("没有最近(昨天以来)日报文件, 跳过发布")
        return

    os.makedirs(docs_dir, exist_ok=True)
    for f in daily:
        src = os.path.join(reports_dir, f)
        dst = os.path.join(docs_dir, f)
        if os.path.exists(dst):
            try:
                old_text = open(dst, encoding="utf-8").read()
                new_text = open(src, encoding="utf-8").read()
                merged = _merge_report(new_text, old_text)
                with open(dst, "w", encoding="utf-8") as fout:
                    fout.write(merged)
                log(f"报告已合并: {f}")
            except Exception as e:
                log(f"⚠️ 合并报告失败({f}), 回退为覆盖: {e}")
                shutil.copy2(src, dst)
        else:
            shutil.copy2(src, dst)
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
    try:
        subprocess.run(["git", "add", "docs/reports", "state"], cwd=WORKDIR,
                       check=True, capture_output=True, timeout=30)
        # 云端 Actions 没有全局 git user, 统一设置(本地也无害)
        _ensure_git_user()
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=WORKDIR,
                              capture_output=True, timeout=30)
        if diff.returncode == 0:
            log("报告无变化, 跳过提交")
            return
        subprocess.run(["git", "commit", "-m", f"docs: update reports [{tag}] [skip ci]"],
                       cwd=WORKDIR, check=True, capture_output=True, timeout=30)
        if _push():
            log("✅ 报告已提交并推送")
    except Exception as e:
        log(f"⚠️ 报告发布失败: {e}")


def sync_state_from_remote():
    """运行前同步远端状态(state/processed.txt, last_bvid.txt), 保证本地和云端一份"""
    try:
        # 丢弃上次残留的本地 state 改动(每次运行前本来就会 reset, 不需要保留)
        subprocess.run(["git", "checkout", "--", "state"], cwd=WORKDIR,
                       capture_output=True, timeout=30)
        r = subprocess.run(["git", "pull", "--ff-only"], cwd=WORKDIR, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=60)
        log(f"同步远端状态: 返回码={r.returncode} stdout={r.stdout.strip()!r} stderr={r.stderr.strip()!r}")
    except Exception as e:
        log(f"⚠️ 同步远端状态失败: {e}")


def reset_local_state():
    """运行前把 last_bvid 置为哨兵, 让 monitor 按'昨天以来+未处理'重新扫描, 避免被旧 last_bvid 卡住"""
    try:
        state_dir = os.path.join(WORKDIR, "state")
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, "last_bvid.txt"), "w", encoding="utf-8") as f:
            f.write("BV0reset")
        log("已重置 last_bvid=BV0reset (重新扫描昨天以来未处理视频)")
    except Exception as e:
        log(f"⚠️ 重置 last_bvid 失败: {e}")


def get_ollama_model():
    try:
        with open(os.path.join(WORKDIR, "config.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg.get("llm_ollama", {}).get("model", "batiai/qwen3.6-35b:iq3")
    except Exception:
        return "batiai/qwen3.6-35b:iq3"


def stop_ollama_model(model):
    """提交报告后停止 Ollama 模型, 释放显存/内存"""
    log(f"命令: ollama stop {model}")
    try:
        r = subprocess.run(["ollama", "stop", model], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30)
        log(f"返回码={r.returncode} stdout={r.stdout.strip()!r} stderr={r.stderr.strip()!r}")
        if r.returncode != 0:
            log("⚠️ 停止 Ollama 模型失败")
        else:
            log("✅ 已停止 Ollama 模型, 释放内存")
    except Exception as e:
        log(f"⚠️ 停止 Ollama 模型异常: {e}")


def run_slot(args):
    today = today_str()
    now_date = datetime.datetime.now(CN_TZ).date()
    dates = [(now_date - datetime.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(3)]  # 前天,昨天,今天
    log(f"[slot] 开始检查 (baseline={dates[0]}, dates={dates})")
    exists = {d: cloud_report_exists(d) for d in dates}
    if all(exists.values()):
        log("✅ 云端已有前天/昨天/今天的报告, 本地跳过")
        return
    for d in dates:
        log(f"ℹ️ 云端{'已有' if exists[d] else '没有'} {d} 报告")
    if cloud_run_in_progress():
        log("☁️ 云端任务进行中, 本地跳过")
        return
    model = get_ollama_model()
    log(f"🚀 云端未处理, 本地开始运行 (Ollama {model})")
    if not set_local_claimed():
        log("❌ 告警: 认领标志重试3次(间隔2秒)仍全部失败, 放弃本次运行 (守护进程继续等待下个时段)")
        return False
    try:
        sync_state_from_remote()   # 先同步云端 state, 本地受制于云端
        reset_local_state()
        run_monitor(args)
        publish_reports(args)
    finally:
        commit_state(args, "local")   # 没日报/异常时也提交 state, 避免脏状态挡下次 pull
        stop_ollama_model(model)   # 提交报告后停止大模型, 释放内存
        clear_local_claimed()
    return True


def main():
    ap = argparse.ArgumentParser(description="本地常驻定时 (程序内循环, 本地优先)")
    ap.add_argument("--once", action="store_true", help="立即执行一次后退出")
    ap.add_argument("--times", default=",".join(DEFAULT_TIMES),
                    help="运行时间(北京时间, 逗号分隔), 默认 14:39,19:59 (比云端早1分钟)")
    ap.add_argument("--grace-minutes", type=int, default=0,
                    help="兼容参数(已不再等待, 保留无效果)")
    ap.add_argument("--config", default=None, help="传给 monitor.py 的配置文件")
    args = ap.parse_args()

    _disable_quickedit()   # 防止控制台 QuickEdit 导致输出冻结

    times = [t.strip() for t in args.times.split(",") if t.strip()]
    log(f"本地常驻启动: 计划 {times} (北京时间), 云端仓库 {REPO} (黑框内按 R 立即运行 / Q 退出)")

    if args.once:
        run_slot(args)
        return 0

    last_logged_next = None
    last_run_slot = None
    try:
        while True:
            try:
                now = datetime.datetime.now(CN_TZ)
                due = find_due_slot(now, times, last_run_slot)
                if due is not None:
                    log(f"⏰ 到点触发: {due.strftime('%Y-%m-%d %H:%M:%S')}")
                    run_slot(args)
                    last_run_slot = due
                    last_logged_next = None
                    continue
                nxt = next_future_slot(now, times)
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
                        # 手动运行覆盖了当前宽限期内的 slot, 记录避免随后自动重复跑
                        now_after = datetime.datetime.now(CN_TZ)
                        due_after = find_due_slot(now_after, times, last_run_slot)
                        if due_after is not None:
                            last_run_slot = due_after
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
