"""文本工具: 日志 / 读写 / 归一化 / OCR 文本过滤 (从 monitor.py 抽出, 逻辑未改)"""
import datetime
import re

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

def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)

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

