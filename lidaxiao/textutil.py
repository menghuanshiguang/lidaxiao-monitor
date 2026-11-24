"""文本工具: 日志 / 读写 / 归一化 / OCR 文本过滤 (从 monitor.py 抽出, 逻辑未改)"""
import datetime
import os
import re
import time

TS_RE = re.compile(r"^\s*(\d{1,2}:\d{2}(:\d{2})?)\s*$")

CLEAN_RE = re.compile(r"[\s\u3000·•\-\|/\\,，。.、;；:：!！?？\"'“”‘’()（）\[\]【】<>《》]+")

WM_RE = re.compile(
    r"(加我粉丝团|求关注|点个关注|关注一下|一键三连|点赞投币|"
    r"点赞|投币|收藏|转发|弹幕|粉丝团|"
    r"bili\w*|LIDAXIAO|"
    r"(?:李大霄|李大海|李大销)\s*(?=bili)|"
    r"\bB站\b)",
    re.I)

def log(msg):
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

def read_text(path, default=""):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return default

TMP_SUFFIX_RE = re.compile(r"\.tmp\.(\d+)$")


def write_text(path, text):
    """原子写: 先写同目录临时文件, fsync 后 os.replace 换上去。

    进程被杀时主文件要么是旧版、要么是新版, **不会留半份**; 残留的
    `<name>.tmp.<pid>` 只在 os.replace 之前被杀时出现, 说明那次写入没生效,
    由 sweep_temp_files() 在下次启动时清掉 —— 不尝试从 tmp 恢复, 因为主文件
    本身是完整的旧版。
    """
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def sweep_temp_files(*dirs, min_age_seconds=600):
    """清掉残留的 `*.tmp.<pid>`; 返回被清掉的路径列表。

    只清「够旧」的: 另一个进程可能正在写自己的 tmp, 抢着删会打断它。
    """
    removed = []
    now = time.time()
    for d in dirs:
        if not d or not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if not TMP_SUFFIX_RE.search(name):
                continue
            full = os.path.join(d, name)
            try:
                if now - os.path.getmtime(full) < min_age_seconds:
                    continue
                os.remove(full)
                removed.append(full)
            except OSError:
                pass
    return removed

def norm_text(s):
    return CLEAN_RE.sub("", s)

def filter_ocr_text(text, conf_threshold):
    """过滤: 低置信度/纯时间戳/过短/行情图表噪声/站内水印"""
    t = text.strip()
    if not t:
        return None
    t = WM_RE.sub(" ", t).strip(" ,，。.、:：|/")
    t = re.sub(r"\s{2,}", " ", t).strip()
    if not t:
        return None
    if TS_RE.match(t):
        return None
    if len(t) < 2:
        return None
    # 纯数字/纯符号行 (常见水印/角标)
    if re.fullmatch(r"[\d\s:/\-—.]+", t):
        return None
    # 行情图表噪声: 一行里数字过多或 % 过多 (口语字幕很少如此密集)
    digits = sum(ch.isdigit() for ch in t)
    pct = t.count("%")
    if len(t) >= 8 and (digits > 6 or pct > 3):
        return None
    return t

