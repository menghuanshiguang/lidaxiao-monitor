# -*- coding: utf-8 -*-
"""本机 Actions 触发器 — 到点补一次 workflow_dispatch, 把云端叫起来。

为什么有这个东西
================
GitHub 的 `schedule` 经常不按时触发(实测偏差可达数十分钟)。本机到点补发一次
dispatch, 由云端去跑去重后的实际工作。

为什么互斥要三层, 缺一不可
==========================
`concurrency: group: lidaxiao-monitor` **做不到「只保持第一个」**: GitHub 的行为是
1 个在跑 + 1 个排队, 第二发会等第一个跑完再跑一遍。拒绝必须发生在**派发端**,
workflow 里没有开关可找。

  1) 本地原子锁 `RunLock`(复用 lidaxiao/runtime/state.py): 跨「预检 -> 派发 ->
     确认 run 出现」全程持有, 不松手
  2) 派发前查 GitHub: `monitor.yml` 有 `queued` / `in_progress` 就跳过, 一发都不发
  3) 本地持久记账 `state/triggered_slots.txt`: 同一 slot 派过就再也不派

第 3 层不能砍: dispatch 返回到 run 出现在 API 之间有秒级延迟, 同秒两个进程触发时
第 2 层查到的都是空, 必须靠本地记账兜。

退出码(响亮失败, 不许静默成功)
==============================
0 该做的都做了, 或本来就不该做
2 无法核验(查询 API 失败/断网): 不发
3 已派发但 60s 内 run 没出现: 不记账, 下次仍会补
4 dispatch 没拿到 204
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lidaxiao.common import CN_TZ, STATE_DIR, WORKDIR  # noqa: E402
from lidaxiao.runtime.state import RunLock  # noqa: E402
from lidaxiao.textutil import log  # noqa: E402

REPO = "menghuanshiguang/lidaxiao-monitor"
WORKFLOW_FILE = "monitor.yml"
WORKFLOW_PATH = ".github/workflows/monitor.yml"
API = "https://api.github.com"
SLOTS = ("14:40", "20:00")          # 时区钉死 CN_TZ, 不许用裸 datetime.now()
GRACE_MINUTES = 60                  # 错过 slot 后 60 分钟内仍补发, 覆盖机器刚睡醒
CONFIRM_WAIT_SECONDS = 60           # 派发后等 run 出现在 API 里的上限
CONFIRM_POLL_SECONDS = 5

LEDGER_PATH = os.path.join(STATE_DIR, "triggered_slots.txt")
LOCK_PATH = os.path.join(WORKDIR, "data", "trigger.lock")


# ---------------------------------------------------------------- 纯函数
def slot_datetimes(day, slots=SLOTS):
    """某一天的各个 slot 时刻(带 CN_TZ)。"""
    out = []
    for s in slots:
        hh, mm = s.split(":")
        out.append(datetime.datetime(day.year, day.month, day.day,
                                     int(hh), int(mm), tzinfo=CN_TZ))
    return out


def slot_key(dt):
    """记账键: 零填充的 "YYYY-MM-DD HH:MM", 字符串比较即时间顺序。"""
    return dt.strftime("%Y-%m-%d %H:%M")


def due_slot(now, grace_minutes=GRACE_MINUTES, slots=SLOTS):
    """最近一个已到点、且仍在补发窗口内的 slot; 没有返回 None。

    只返回**一个**: 一夜没开机醒来时, 晚的那个 slot 优先, 早的那个不补,
    免得一次苏醒连发两发把云端排成两轮。
    """
    days = [now.date(), now.date() - datetime.timedelta(days=1)]
    cands = [s for d in days for s in slot_datetimes(d, slots)]
    passed = [s for s in cands if s <= now]
    if not passed:
        return None
    last = max(passed)
    if (now - last).total_seconds() > grace_minutes * 60:
        return None
    return last


def ledger_entries(path):
    """[("2026-11-24 14:40", "run_id"), ...]; 跳过注释与空行。"""
    out = []
    if not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8-sig") as f:
        for ln in f:
            s = ln.strip()
            if not s or s.startswith("#"):
                continue
            parts = s.split("|")
            out.append((parts[0].strip(), parts[1].strip() if len(parts) > 1 else ""))
    return out


def ledger_keys(path):
    return {k for k, _ in ledger_entries(path)}


def ledger_latest_key(path):
    keys = [k for k in ledger_keys(path) if k]
    return max(keys) if keys else ""


def ledger_add(path, key, run_id="", note=""):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    stamp = datetime.datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
    line = f"{key}|{run_id}|{stamp}"
    if note:
        line += f"|{note}"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    return line


def should_skip(slot_dt, path):
    """(是否跳过, 原因)。记账命中 / 时间被往回拨 -> 跳过。"""
    key = slot_key(slot_dt)
    keys = ledger_keys(path)
    if key in keys:
        return True, f"本 slot 已派过({key}), 不再派"
    latest = max([k for k in keys if k], default="")
    if latest and latest > key:
        return True, (f"记账里最新的 slot 是 {latest}, 比本次 {key} 还晚 —— "
                      f"系统时间被往回拨过, 不派")
    return False, ""


def remote_busy(runs, workflow_path=WORKFLOW_PATH):
    """云端有 queued/in_progress 的 monitor.yml run 吗; 有则返回那条 run。"""
    for r in runs or []:
        if r.get("path") == workflow_path and r.get("status") in ("queued", "in_progress"):
            return r
    return None


def run_matches(run, since_dt, workflow_path=WORKFLOW_PATH):
    """这条 run 是不是「我刚才派发的那一发」。"""
    if run.get("path") != workflow_path:
        return False
    if run.get("created_at"):
        try:
            created = datetime.datetime.fromisoformat(
                run["created_at"].replace("Z", "+00:00"))
        except ValueError:
            return False
        # 允许 120s 时钟偏差: 跑 dispatch 的机器与 GitHub 的时钟不会完全一致
        if created < since_dt - datetime.timedelta(seconds=120):
            return False
    return True


# ---------------------------------------------------------------- HTTP
def _http(method, url, token="", body=None, timeout=30):
    """返回 (status, 文本)。不抛异常: 网络错误返回 (0, 原因)。绝不回显 token。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "lidaxiao-trigger")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return getattr(r, "status", 200), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception:
            return e.code, ""
    except Exception as e:                 # 断网/DNS/TLS/超时
        return 0, f"{type(e).__name__}: {e}"


def fetch_dispatched_runs(fetch, token):
    """最近 10 条 workflow_dispatch run。返回 (status, runs 或 None)。"""
    url = f"{API}/repos/{REPO}/actions/runs?event=workflow_dispatch&per_page=10"
    status, text = fetch("GET", url, token)
    if status != 200:
        return status, None
    try:
        return status, (json.loads(text or "{}").get("workflow_runs") or [])
    except ValueError:
        return status, None


def post_dispatch(fetch, token, ref="main"):
    url = f"{API}/repos/{REPO}/actions/workflows/{WORKFLOW_FILE}/dispatches"
    status, _ = fetch("POST", url, token, {"ref": ref, "inputs": {"force": "true"}})
    return status


def wait_for_run(fetch, token, since_dt, wait_seconds=CONFIRM_WAIT_SECONDS,
                 poll_seconds=CONFIRM_POLL_SECONDS, sleep=time.sleep,
                 clock=time.monotonic):
    """等派发出来的 run 真的出现在 API 里; 返回 run 或 None。

    clock 可注入: 测试里不能真等 60 秒, 否则一条用例就把整轮测试拖成分钟级。
    """
    deadline = clock() + wait_seconds
    while True:
        status, runs = fetch_dispatched_runs(fetch, token)
        if runs:
            for r in runs:
                if run_matches(r, since_dt):
                    return r
        if clock() >= deadline:
            return None
        sleep(poll_seconds)


# ---------------------------------------------------------------- token
def load_token():
    """(token, 来源说明)。来源说明只写来源, 不写值。"""
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        val = (os.environ.get(name) or "").strip()
        if val:
            return val, f"env {name}"
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True,
                             text=True, timeout=20)
        if out.returncode == 0 and (out.stdout or "").strip():
            return out.stdout.strip(), "gh auth token"
    except Exception:
        pass
    path = (os.environ.get("LIDAXIAO_GH_TOKEN_FILE")
            or os.path.join(os.path.dirname(WORKDIR), ".creds", "gh_token"))
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8-sig") as f:
                val = f.read().strip()
            if val:
                return val, "creds 文件"
    except OSError:
        pass
    return "", ""


# ---------------------------------------------------------------- 主流程
def main(argv=None, fetch=None, now=None, ledger_path=None, lock_path=None,
         sleep=time.sleep, clock=time.monotonic):
    ap = argparse.ArgumentParser(description="到点补发一次 monitor.yml dispatch")
    ap.add_argument("--now", default=None,
                    help="用指定时刻代替当前时间(ISO, 用于演练/诊断), 不影响记账以外的行为")
    ap.add_argument("--dry-run", action="store_true", help="只报告决定, 不派发、不记账")
    args = ap.parse_args(argv)

    fetch = fetch or _http
    ledger_path = ledger_path or LEDGER_PATH
    lock_path = lock_path or LOCK_PATH

    if now is None:
        now = datetime.datetime.fromisoformat(args.now) if args.now else \
            datetime.datetime.now(CN_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=CN_TZ)
    else:
        now = now.astimezone(CN_TZ)

    slot = due_slot(now)
    if slot is None:
        log(f"⏰ {now.strftime('%Y-%m-%d %H:%M')} 不在任何 slot 的补发窗口内, 不发")
        return 0
    key = slot_key(slot)
    log(f"⏰ 本次目标 slot: {key} (现在 {now.strftime('%Y-%m-%d %H:%M')})")

    skip, why = should_skip(slot, ledger_path)
    if skip:
        log(f"⏭ 跳过: {why}")
        return 0

    if args.dry_run:
        log(f"🧪 dry-run: 本应派发 {WORKFLOW_FILE} (ref=main, force=true), 未执行")
        return 0

    token, src = load_token()
    if not token:
        log("❌ 未找到 GitHub token (GH_TOKEN / GITHUB_TOKEN / gh auth token / .creds/gh_token)")
        return 2
    log(f"🔑 token 来源: {src}")

    lock = RunLock(lock_path)
    if not lock.acquire():
        log("⏭ 另一个 trigger 实例正在运行, 本次不动")
        return 0
    try:
        # 记账要在锁里再读一次: 两个进程可能都过了上面的检查
        skip, why = should_skip(slot, ledger_path)
        if skip:
            log(f"⏭ 跳过(锁内复查): {why}")
            return 0

        status, runs = fetch_dispatched_runs(fetch, token)
        if runs is None:
            log(f"❌ 查不到云端 run (HTTP {status}), 无法判断是否已有在跑, 本次不发")
            return 2
        busy = remote_busy(runs)
        if busy:
            log(f"⏭ 云端已有 run {busy.get('id')} ({busy.get('status')}), 本次 0 派发")
            return 0

        since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=5)
        status = post_dispatch(fetch, token)
        if status != 204:
            log(f"❌ dispatch 未返回 204 (HTTP {status}), 视为失败")
            return 4
        log("📤 dispatch 已接受 (204), 等 run 出现在 API 里...")

        run = wait_for_run(fetch, token, since, sleep=sleep, clock=clock)
        if not run:
            log(f"❌ 派发后 {CONFIRM_WAIT_SECONDS}s 内没等到 run, 不记账(下次仍会补)")
            return 3
        rid = run.get("id")
        ledger_add(ledger_path, key, str(rid))
        log(f"✅ slot {key} 已派发并确认: run {rid} status={run.get('status')}")
        return 0
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
