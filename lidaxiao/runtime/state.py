"""状态与并发锁: processed.txt / 运行锁 — 从 monitor.py 抽出

依赖只到 common 与 textutil; 不 import 上层任何模块。
"""
import datetime
import os
import re
import time

from lidaxiao.common import CN_TZ, STATE_DIR
from lidaxiao.textutil import log, write_text

LOCK_STALE_SECONDS = 8 * 3600


def read_state_lines(path):
    lines = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    lines.append(line)
    return lines


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class RunLock:
    def __init__(self, path):
        self.path = path
        self.fd = None

    def acquire(self):
        for _ in range(2):
            try:
                self.fd = os.open(self.path,
                                  os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, f"pid={os.getpid()} start={time.time()}".encode())
                return True
            except FileExistsError:
                # 先看锁里记录的 PID 是否还活着: 死了就是残留锁, 直接接管
                try:
                    with open(self.path, "r", encoding="utf-8") as f:
                        content = f.read()
                    m = re.search(r"pid=(\d+)", content)
                    if m:
                        pid = int(m.group(1))
                        if not _pid_alive(pid):
                            log(f"检测到死进程锁(pid={pid}), 强制接管")
                            try:
                                os.remove(self.path)
                            except OSError:
                                pass
                            continue
                except Exception:
                    pass
                try:
                    age = time.time() - os.path.getmtime(self.path)
                except OSError:
                    age = 0
                if age > LOCK_STALE_SECONDS:
                    log(f"检测到陈旧锁文件({int(age)}秒), 强制接管")
                    try:
                        os.remove(self.path)
                    except OSError:
                        pass
                    continue
                log(f"另一实例正在运行(锁: {self.path}), 退出")
                return False
        return False

    def release(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        try:
            os.remove(self.path)
        except OSError:
            pass


def mark_processed(bvid, title, status):
    os.makedirs(STATE_DIR, exist_ok=True)
    p = os.path.join(STATE_DIR, "processed.txt")
    stamp = datetime.datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
    line = f"{bvid}|{stamp}|{status}|{title}"
    keep = []
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8-sig") as f:
            for ln in f:
                s = ln.strip()
                if not s or s.startswith("#"):
                    continue
                if s.split("|", 1)[0].strip() != bvid:
                    keep.append(s)
    keep.append(line)
    write_text(p, "\n".join(keep) + "\n")          # 原子写: 被杀不留半份 processed.txt


def processed_bvids():
    return {l.split("|")[0] for l in read_state_lines(
        os.path.join(STATE_DIR, "processed.txt"))}


def processed_status_map():
    """bvid -> 最近一次处理状态 (ok/partial/error/no-subtitle)"""
    out = {}
    for l in read_state_lines(os.path.join(STATE_DIR, "processed.txt")):
        parts = l.split("|")
        if len(parts) >= 3:
            out[parts[0]] = parts[2]
    return out


def is_success_status(status):
    """视为成功、不需要自动重试的状态"""
    return status in ("ok", "no-subtitle")


def is_from_baseline(video, days=2):
    """是否北京时间 baseline(默认前天)及之后发布的视频, 更早的忽略"""
    ts = video.get("pubdate") or video.get("created")
    if not ts:
        return False
    d = datetime.datetime.fromtimestamp(ts, CN_TZ).date()
    baseline = datetime.datetime.now(CN_TZ).date() - datetime.timedelta(days=days)
    return d >= baseline
