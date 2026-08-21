#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
李大霄视频自动监控分析系统 - 主脚本 (Windows)
====================================================
功能:
  1. 更新检测  : bilidown space 获取 UP主最新视频, 与 data/last_bvid.txt 对比
  2. 视频下载  : bilidown dl 下载 480P mp4 (失败重试1次)
  3. 字幕提取  : ffmpeg 抽帧 + RapidOCR 硬字幕识别 (自适应字幕区域 + 相邻帧去重)
  4. AI 分析   : OpenCode Zen Go API (deepseek-v4-flash) 对字幕做"李大霄话术解码"分析
  5. 每日报告  : reports/YYYY-MM-DD.md (当天已有报告则追加)
  6. 状态幂等  : data/last_bvid.txt + data/processed.txt + 并发锁

用法:
  python monitor.py            # 正常运行 (无新视频秒退)
  python monitor.py --force    # 强制重新处理最新视频
  python monitor.py --uid 2137589551 --limit 10
  python monitor.py --config config.json

退出码: 0=正常(含无更新)  1=出错
"""
import argparse
import datetime
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request

# ---------- 常量 ----------
WORKDIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(WORKDIR, "state")   # 云端/本地共享状态, 入库同步
# bash 路径可用环境变量 BILIDOWN_BASH 覆盖 (如 WSL/GitBash 安装在不同位置)
BASH = os.environ.get("BILIDOWN_BASH", r"D:\Program Files\Git\bin\bash.exe")
BILIDOWN_SCRIPT = os.path.join(WORKDIR, "_repo", "bin", "bilidown")
FFMPEG_DIR = os.path.join(WORKDIR, "tools", "ffmpeg", "bin")


def _find_tool(name):
    """查找 ffmpeg/ffprobe: 环境变量 LIDAXIAO_<NAME> > tools/ > PATH"""
    p = os.environ.get(f"LIDAXIAO_{name.upper()}", "").strip()
    if p and os.path.exists(p):
        return p
    cand = os.path.join(FFMPEG_DIR, f"{name}.exe")
    if os.path.exists(cand):
        return cand
    w = shutil.which(name)
    return w or name


FFMPEG = _find_tool("ffmpeg")
FFPROBE = _find_tool("ffprobe")
DEFAULT_UID = "2137589551"          # 李大霄
CN_TZ = datetime.timezone(datetime.timedelta(hours=8))
DISCLAIMER = "以上为李大霄个人观点提炼,不构成投资建议"
LOCK_STALE_SECONDS = 8 * 3600       # 锁文件超过8小时视为陈旧
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def log(msg):
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# =====================================================================
# 配置 / 密钥
# =====================================================================
def load_config(path=None):
    cfg = {
        "uid": DEFAULT_UID,
        "limit": 10,                    # 每次最多检查/处理多少条
        "frame_interval": 0.0,          # 0=自适应 (时长/240, 限1~4秒)
        "ocr_confidence": 0.5,
        "min_subtitle_chars": 30,       # 字幕文本少于该长度视为"无字幕"
        "llm": {
            "provider": "opencode-go",          # OpenCode Zen Go (https://opencode.ai/zen/go)
            "base_url": "https://opencode.ai/zen/go/v1",
            "model": "deepseek-v4-flash",
            "reasoningEffort": "max",           # 推理等级: off/minimal/low/medium/high/max (deepseek-v4-flash 支持 high/max)
            "temperature": 0.3,
            "max_tokens": 8000,
        },
        "llm_fallback": {                       # OpenCode Go 失败时的 DeepSeek 兜底
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-chat",
            "temperature": 0.3,
            "max_tokens": 4000,
        },
        "llm_cli": {                            # 第二兜底: deepseek-chat-cli (DeepSeek 网页版登录 token)
            "provider": "deepseek-chat-cli",
            "repo": "https://github.com/menghuanshiguang/deepseek-chat-cli.git",
            "path": "_repo/deepseek-chat-cli",
            "token_env": "DSV_TOKEN",
            "timeout": 300,
        },
        "llm_ollama": {                         # 本地模式: Ollama (无 key)
            "provider": "ollama",
            "base_url": "http://localhost:11434/v1",
            "model": "batiai/qwen3.6-35b:iq3",
            "temperature": 0.3,
            "max_tokens": 8000,
            "timeout": 600,
        },
        "dl_retries": 1,                # 下载失败额外重试次数
        "llm_retries": 3,
    }
    p = path or os.path.join(WORKDIR, "config.json")
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            user = json.load(f)
        cfg.update(user)
        cfg["llm"].update(user.get("llm", {}))
        cfg["llm_fallback"].update(user.get("llm_fallback", {}))
        cfg["llm_cli"].update(user.get("llm_cli", {}))
        cfg["llm_ollama"].update(user.get("llm_ollama", {}))
    # 本地模式开关: 环境变量 LIDAXIAO_LOCAL=1 或 config.json local_ollama=true
    cfg["local_ollama"] = bool(user.get("local_ollama", False)) if os.path.exists(p) else False
    if os.environ.get("LIDAXIAO_LOCAL") == "1":
        cfg["local_ollama"] = True
    return cfg


LLM_PROVIDER_KEY_ENVS = {
    "opencode-go": ("OPENCODE_GO_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "deepseek-chat-cli": ("DSV_TOKEN", "DEEPSEEK_CHAT_CLI_TOKEN"),
}
LLM_KEY_ENVS = ("OPENCODE_GO_API_KEY", "DEEPSEEK_API_KEY", "DSV_TOKEN")


def _env_or_envfile(name):
    """从环境变量或工作目录 .env 文件读取单个密钥"""
    v = os.environ.get(name, "").strip()
    if v:
        return v
    envf = os.path.join(WORKDIR, ".env")
    if os.path.exists(envf):
        with open(envf, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(name):
                    return line.split("=", 1)[1].strip().strip("'\"")
    return ""


def load_llm_key(provider=None):
    """密钥优先级: 环境变量 > .env 文件; provider=None 时按 OPENCODE_GO_API_KEY > DEEPSEEK_API_KEY 顺序"""
    names = LLM_PROVIDER_KEY_ENVS.get(provider, LLM_KEY_ENVS) if provider else LLM_KEY_ENVS
    for name in names:
        v = _env_or_envfile(name)
        if v:
            return v
    return ""


def load_deepseek_key():
    """向后兼容别名 (旧脚本/旧密钥名仍可用)"""
    return load_llm_key()


# =====================================================================
# 工具函数
# =====================================================================
def win_to_msys(path):
    """C:\\a\\b -> /c/a/b (bilidown bash 脚本要求 POSIX 绝对路径)"""
    p = path.replace("\\", "/")
    m = re.match(r"^([A-Za-z]):(/.*)$", p)
    if m:
        return "/" + m.group(1).lower() + m.group(2)
    return p


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


def read_state_lines(path):
    lines = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    lines.append(line)
    return lines


# ---------------- 并发锁 ----------------
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


# ---------------- bilidown 调用 ----------------
def run_bilidown(cfg, args, timeout=600):
    """调用 bilidown CLI: 优先 v2.0+ Python 单文件版, 兼容旧 bash 版"""
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join([FFMPEG_DIR, env.get("PATH", "")])
    py_script = os.path.join(WORKDIR, "_repo", "bin", "bilidown.py")
    if os.path.exists(py_script):
        cmd = [sys.executable, py_script] + args          # v2.0+: python bilidown.py
        log(f"运行: bilidown {' '.join(args)}")
    elif os.path.exists(BILIDOWN_SCRIPT):
        env["HOME"] = os.path.expanduser("~")
        extra = [os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "Python",
                              "Python312", "Scripts")]
        env["PATH"] = os.pathsep.join(extra + [env["PATH"]])
        cmd = [BASH, BILIDOWN_SCRIPT] + args              # 旧版: bash bin/bilidown
        log(f"运行: bilidown(bash) {' '.join(args)}")
    else:
        log("❌ 未找到 bilidown: 请克隆 https://github.com/menghuanshiguang/bilibili-downloader-cli 到 _repo/")
        return 127, "", ""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        log(f"bilidown 超时({timeout}s): {args}")
        return 124, "", ""
    return r.returncode, r.stdout, r.stderr


# ---------------- B站 API (view 信息) ----------------
def load_cookies_dict():
    """从 data/cookies.txt 或 ~/.cache/bilibili-login-cookies.txt 读 Netscape cookies"""
    paths = [os.path.join(WORKDIR, "data", "cookies.txt"),
             os.path.join(os.path.expanduser("~"), ".cache",
                          "bilibili-login-cookies.txt")]
    ck = {}
    for p in paths:
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split("\t")
                    if len(parts) >= 7:
                        ck[parts[5]] = parts[6]
            if ck:
                break
    return ck


def bili_api_get(url, params=None, referer=None):
    ck = load_cookies_dict()
    qs = urllib.parse.urlencode(params or {})
    full = url + ("?" + qs if qs else "")
    req = urllib.request.Request(full, headers={
        "User-Agent": UA,
        "Referer": referer or "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
        "Cookie": "; ".join(f"{k}={v}" for k, v in ck.items()),
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log(f"B站API请求失败 {url}: {e}")
        return None


def fetch_video_info(bvid):
    """GET /x/web-interface/view -> title/pubdate/duration"""
    j = bili_api_get("https://api.bilibili.com/x/web-interface/view",
                     {"bvid": bvid})
    if j and j.get("code") == 0 and j.get("data"):
        d = j["data"]
        return {
            "title": d.get("title", ""),
            "pubdate": d.get("pubdate", 0),
            "duration": d.get("duration", 0),
        }
    return None


# =====================================================================
# 1. 更新检测
# =====================================================================
SPACE_ITEM_RE = re.compile(r"^\s*(\d+)\.\s*\[([\d:]+)\]\s*(.+?)\s*$")
SPACE_BV_RE = re.compile(r"^\s*BV:\s*([A-Za-z0-9]+)")


def get_space_videos(cfg, count):
    """bilidown space -> [{bvid,title,dur_str,pubdate}] 最新在前"""
    rc, out, err = run_bilidown(cfg, ["space", cfg["uid"], str(count)], timeout=120)
    if rc != 0:
        log(f"space 命令失败 rc={rc}\nstderr: {err[-500:]}")
        return []
    videos = []
    cur = None
    for line in out.splitlines():
        m = SPACE_ITEM_RE.match(line)
        if m:
            cur = {"bvid": "", "title": m.group(3).strip(), "dur_str": m.group(2)}
            videos.append(cur)
            continue
        m2 = SPACE_BV_RE.match(line)
        if m2 and cur is not None:
            cur["bvid"] = m2.group(1)
    result = [v for v in videos if v["bvid"]]
    # 补 pubdate: 日期过滤需要 (bilidown space 不返回时间)
    for v in result:
        info = fetch_video_info(v["bvid"])
        if info:
            v["pubdate"] = info.get("pubdate", 0)
            v["title"] = info.get("title", v.get("title", ""))
        else:
            v["pubdate"] = 0
    return result


# =====================================================================
# 2. 视频下载
# =====================================================================
def find_downloaded_mp4(bvid):
    d = os.path.join(WORKDIR, "downloads", bvid)
    if not os.path.isdir(d):
        return None
    for f in sorted(glob.glob(os.path.join(d, "*.mp4"))):
        if not os.path.basename(f).startswith("."):
            return f
    return None


def download_video(cfg, bvid, retries=None):
    """bilidown dl <BV> video <outdir> mp4 480 1, 返回本地mp4路径或None"""
    retries = cfg["dl_retries"] if retries is None else retries
    outdir = os.path.join(WORKDIR, "downloads", bvid)
    attempts = 1 + retries
    for i in range(attempts):
        existing = find_downloaded_mp4(bvid)
        if existing:
            log(f"视频已存在, 跳过下载: {existing}")
            return existing
        rc, out, err = run_bilidown(
            cfg, ["dl", bvid, "video", outdir, "mp4", "480", "1"],
            timeout=900)
        ok = rc == 0
        if not ok:
            log(f"下载失败(第{i+1}/{attempts}次) rc={rc}\n{err[-600:]}")
            if i + 1 < attempts:
                time.sleep(5)
            continue
        existing = find_downloaded_mp4(bvid)
        if existing:
            log(f"✅ 下载完成: {existing}")
            return existing
        log(f"bilidown 返回成功但未找到mp4 (第{i+1}次)\n{out[-300:]}\n{err[-300:]}")
    return None


# =====================================================================
# 3. 字幕提取 (ffmpeg 抽帧 + RapidOCR)
# =====================================================================
_ocr = None


def get_ocr():
    global _ocr
    if _ocr is None:
        from rapidocr_onnxruntime import RapidOCR
        # 优先 DirectML (RTX 4060 GPU 加速); 不可用时自动回退 CPU
        try:
            _ocr = RapidOCR(intra_op_num_threads=8,
                            det_use_dml=True, cls_use_dml=True, rec_use_dml=True)
            if _ocr.text_det.infer.session.get_providers()[0] != "DmlExecutionProvider":
                raise RuntimeError("DML provider not active")
        except Exception:
            log("DirectML 不可用, 回退 CPU OCR")
            _ocr = RapidOCR(intra_op_num_threads=8)
    return _ocr


def ffprobe_duration(video_path):
    try:
        r = subprocess.run([FFPROBE, "-v", "error", "-show_entries",
                            "format=duration", "-of", "csv=p=0", video_path],
                           capture_output=True, text=True, timeout=60)
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def extract_frames(video_path, bvid, interval):
    """ffmpeg 按 interval 秒抽帧到 data/frames/<bvid>/"""
    fdir = os.path.join(WORKDIR, "data", "frames", bvid)
    if os.path.isdir(fdir) and glob.glob(os.path.join(fdir, "*.jpg")):
        return sorted(glob.glob(os.path.join(fdir, "*.jpg")))
    os.makedirs(fdir, exist_ok=True)
    pat = os.path.join(fdir, "f_%06d.jpg")
    cmd = [FFMPEG, "-y", "-v", "error", "-i", video_path,
           "-vf", f"fps=1/{interval:.4f}", "-q:v", "4", pat]
    log(f"抽帧: interval={interval}s -> {fdir}")
    try:
        subprocess.run(cmd, check=True, timeout=3600,
                       capture_output=True, text=True)
    except Exception as e:
        log(f"ffmpeg 抽帧失败: {e}")
        return []
    return sorted(glob.glob(os.path.join(fdir, "*.jpg")))


def ocr_frame(engine, img_path, band=None):
    """band=(y0,y1) 0~1 相对高度, None=整帧"""
    import cv2
    img = cv2.imread(img_path)
    if img is None:
        return []
    h, w = img.shape[:2]
    if band:
        y0, y1 = int(h * band[0]), int(h * band[1])
        img = img[max(0, y0):max(0, y1), :]
    result, _ = engine(img)
    boxes = []
    if result:
        for item in result:
            try:
                box, text, score = item[0], item[1], item[2]
            except Exception:
                continue
            ys = [p[1] for p in box]
            boxes.append((min(ys), min(p[0] for p in box), text, float(score)))
    boxes.sort(key=lambda t: (t[0] // 20, t[1]))
    return boxes


TS_RE = re.compile(r"^\s*(\d{1,2}:\d{2}(:\d{2})?)\s*$")
CLEAN_RE = re.compile(r"[\s\u3000·•\-\|/\\,，。.、;；:：!！?？\"'“”‘’()（）\[\]【】<>《》]+")


def norm_text(s):
    return CLEAN_RE.sub("", s)


def filter_ocr_text(text, conf_threshold):
    """过滤: 低置信度/纯时间戳/过短/行情图表噪声"""
    t = text.strip()
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


def probe_subtitle_band(engine, frames, conf):
    """探测字幕带 (y0,y1): 排除顶部logo/右上角水印, 优先底部字幕带"""
    import cv2
    img = cv2.imread(frames[0])
    if img is None:
        return None
    h, w = img.shape[:2]
    ys = []
    for f in frames[:min(12, len(frames))]:
        for y, x, text, score in ocr_frame(engine, f):
            if score >= conf:
                # 排除右上角日期水印(x>0.82w)与顶部 logo(y<0.10h)
                if x < 0.82 * w and y > 0.10 * h:
                    ys.append(y)
    if not ys:
        return None
    bottom = [y for y in ys if y > 0.60 * h]
    if len(bottom) >= max(1, len(ys) // 4):
        band = (0.70, 0.99)
        log(f"字幕带探测: 底部字幕带 ({band[0]:.2f},{band[1]:.2f})")
        return band
    med = sorted(ys)[len(ys) // 2] / h
    y0 = max(0.35, min(med - 0.13, 0.80))
    y1 = min(1.0, y0 + 0.30)
    log(f"字幕带探测: 中位y={med:.2f}, 带=({y0:.2f},{y1:.2f})")
    return (y0, y1)


def ocr_video(cfg, video_path, bvid):
    """返回 (字幕文本, 统计) ; 文本为空表示无字幕"""
    from difflib import SequenceMatcher
    engine = get_ocr()
    conf = cfg["ocr_confidence"]
    dur = ffprobe_duration(video_path)
    interval = cfg.get("frame_interval") or max(1.0, min(4.0, dur / 160.0))
    frames = extract_frames(video_path, bvid, interval)
    if not frames:
        log("抽帧失败, 无帧可OCR")
        return "", {"frames": 0}
    stats = {"frames": len(frames), "ocr_ok": 0, "ocr_empty": 0}

    def scan(frames_idx, band):
        """对给定帧序列做 OCR, 返回 [(ts, line)]"""
        out = []
        last_norm = ""
        for i in frames_idx:
            boxes = ocr_frame(engine, frames[i], band)
            if not boxes:
                continue
            parts = []
            for _, _, text, score in boxes:
                t = filter_ocr_text(text, conf)
                if t and score >= conf:
                    parts.append(t)
            if not parts:
                continue
            line = " ".join(parts)
            n = norm_text(line)
            # 相邻帧去重 (完全相同或高度相似, 如图表帧)
            if n == last_norm or (last_norm and
                                  SequenceMatcher(None, n, last_norm).ratio() > 0.85):
                continue
            last_norm = n
            out.append((int(i * interval), line))
        return out

    # 1) 探测字幕带
    band = probe_subtitle_band(engine, frames, conf)

    # 2) 主扫描
    out_lines = scan(range(len(frames)), band)
    log(f"主扫描完成: 有效 {len(out_lines)} 条/{len(frames)} 帧")

    # 3) 字幕太少 -> 兜底: 底部带每2帧 -> 整帧每6帧
    total = sum(len(l) for _, l in out_lines)
    if total < cfg["min_subtitle_chars"]:
        log("主扫描文本过少, 底部带兜底扫描...")
        extra = scan(range(0, len(frames), 2), (0.72, 0.99))
        out_lines = merge_ts_lines(out_lines, extra)
        total = sum(len(l) for _, l in out_lines)
        if total < cfg["min_subtitle_chars"]:
            log("底部带仍不足, 整帧兜底扫描...")
            extra = scan(range(0, len(frames), 6), None)
            out_lines = merge_ts_lines(out_lines, extra)
            total = sum(len(l) for _, l in out_lines)
        log(f"兜底后 {len(out_lines)} 条, 共 {total} 字")

    if total < cfg["min_subtitle_chars"]:
        return "", stats

    # 4) 落盘持久化
    text = "\n".join(f"[{ts//60:02d}:{ts%60:02d}] {line}" for ts, line in out_lines)
    sub_path = os.path.join(WORKDIR, "data", "subtitles", f"{bvid}.txt")
    write_text(sub_path, text)
    log(f"字幕已保存: {sub_path} ({total}字)")
    return text, stats


def merge_ts_lines(a, b):
    m = {ts: t for ts, t in a}
    for ts, t in b:
        if ts not in m:
            m[ts] = t
    return sorted(m.items())


# =====================================================================
# 4. AI 分析
# =====================================================================
def _call_llm_once(pcfg, api_key, system, user):
    """向单个 LLM provider 发起一次 Chat Completions 请求"""
    import requests
    url = pcfg["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": pcfg["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": pcfg.get("max_tokens", 4000),
        "stream": False,
    }
    # Ollama: 透传 MoE/批处理/缓存参数, 等价于 llama-server 的对应 flag
    if pcfg.get("provider") == "ollama":
        ollama_options = {}
        option_map = {
            "n_cpu_moe": "num_cpu_moe",
            "num_batch": "num_batch",
            "num_ubatch": "num_ubatch",
            "cache_ram": "cache_ram",
        }
        for cfg_key, api_key in option_map.items():
            val = pcfg.get(cfg_key)
            if val:
                ollama_options[api_key] = val
        if ollama_options:
            payload["options"] = ollama_options
    effort = (pcfg.get("reasoningEffort") or "").strip()
    if effort:
        # 推理模型 (如 deepseek-v4-flash): OpenCode Zen Go 的 deepseek 思维格式
        payload["thinking"] = {"type": "enabled"}
        payload["reasoning_effort"] = effort
        # 思维链模式不接受 temperature, 省略以免被端点拒绝
    else:
        payload["temperature"] = pcfg.get("temperature", 0.3)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    r = requests.post(url, json=payload, timeout=pcfg.get("timeout", 180), headers=headers)
    r.raise_for_status()
    j = r.json()
    return j["choices"][0]["message"]["content"].strip()


def _find_dsc_path(pcfg):
    """定位 deepseek-chat-cli 的 dsc.py"""
    rel = pcfg.get("path", "_repo/deepseek-chat-cli")
    cands = []
    if os.path.isabs(rel):
        cands.append(rel)
    else:
        cands.append(os.path.join(WORKDIR, rel))
    for extra in ("_repo/deepseek-chat-cli", "tools/deepseek-chat-cli", "deepseek-chat-cli"):
        cands.append(os.path.join(WORKDIR, extra))
    for d in cands:
        f = os.path.join(d, "dsc.py")
        if os.path.isfile(f):
            return f
    return None


def _repo_slug(repo):
    s = repo.rstrip("/").removesuffix(".git")
    parts = s.split("/")
    return "/".join(parts[-2:])


def _clone_dsc(pcfg):
    """克隆 deepseek-chat-cli (私有仓库, 优先 gh 认证, 其次 git + token)"""
    repo = pcfg.get("repo", "https://github.com/menghuanshiguang/deepseek-chat-cli.git")
    rel = pcfg.get("path", "_repo/deepseek-chat-cli")
    dest = rel if os.path.isabs(rel) else os.path.join(WORKDIR, rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    # 1) gh repo clone (本地已 gh 登录时最稳)
    try:
        subprocess.run(["gh", "repo", "clone", _repo_slug(repo), dest],
                       check=True, capture_output=True, timeout=180)
        return True
    except Exception as e:
        log(f"⚠️ gh clone 失败: {e}")
    # 2) git clone + token
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if not token:
        try:
            token = subprocess.run(["gh", "auth", "token"], capture_output=True,
                                   text=True, timeout=30).stdout.strip()
        except Exception:
            token = ""
    try:
        if token:
            auth_url = repo.replace("https://", f"https://oauth2:{token}@")
            subprocess.run(["git", "clone", "--depth", "1", auth_url, dest],
                           check=True, capture_output=True, timeout=180)
        else:
            subprocess.run(["git", "clone", "--depth", "1", repo, dest],
                           check=True, capture_output=True, timeout=180)
        return True
    except Exception as e:
        log(f"⚠️ git clone 失败: {e}")
    return False


def _call_dsc(pcfg, token, prompt):
    """调用 deepseek-chat-cli (dsc.py): 网页版登录 token 走浏览器对话"""
    dsc_py = _find_dsc_path(pcfg)
    if not dsc_py:
        log("⏬ deepseek-chat-cli 未找到, 尝试克隆...")
        if not _clone_dsc(pcfg):
            raise RuntimeError("deepseek-chat-cli 克隆失败")
        dsc_py = _find_dsc_path(pcfg)
        if not dsc_py:
            raise RuntimeError("克隆后仍未找到 dsc.py")

    tok = (token or "").strip().lstrip("\ufeff")
    if not tok:
        # 兜底: 直接读仓库自带的 .dsv_token
        repo_tok = os.path.join(os.path.dirname(dsc_py), ".dsv_token")
        if os.path.exists(repo_tok):
            with open(repo_tok, "r", encoding="utf-8-sig") as f:
                tok = f.read().strip()
    if not tok:
        raise RuntimeError("deepseek-chat-cli: 未找到 DSV_TOKEN")

    # dsc.py 固定读 ~/.dsv_token, 把系统变量写进去
    token_path = os.path.expanduser("~/.dsv_token")
    with open(token_path, "w", encoding="utf-8") as f:
        f.write(tok)

    # 根因修复: 每次调用使用全新浏览器 profile, 避免连续多次后 dsc 检测退化导致超时
    import uuid
    profile_dir = os.path.join(WORKDIR, "data", "dsc_profiles", uuid.uuid4().hex)
    os.makedirs(profile_dir, exist_ok=True)

    cmd = [sys.executable, os.path.abspath(dsc_py), prompt]
    log(f"调用 deepseek-chat-cli ({os.path.basename(dsc_py)})...")
    # dsc.py 支持 DSC_TIMEOUT 环境变量控制回答等待时长; DSV_EDGE_PROFILE 指定本次全新 profile
    env = dict(os.environ)
    env["DSC_TIMEOUT"] = str(pcfg.get("timeout", 300))
    env["DSV_EDGE_PROFILE"] = profile_dir
    try:
        res = subprocess.run(cmd, cwd=os.path.dirname(dsc_py), capture_output=True,
                             text=True, encoding="utf-8", errors="replace",
                             timeout=pcfg.get("timeout", 300) + 60, env=env)
    except subprocess.TimeoutExpired:
        raise RuntimeError("deepseek-chat-cli 调用超时")
    finally:
        # 用完即删, 避免 data/dsc_profiles 无限膨胀
        shutil.rmtree(profile_dir, ignore_errors=True)
    if res.returncode != 0:
        raise RuntimeError(
            f"deepseek-chat-cli 失败 exit={res.returncode}: {(res.stderr or res.stdout)[-500:]}")
    out = (res.stdout or "").strip()
    if not out:
        raise RuntimeError("deepseek-chat-cli 返回空内容")
    # dsc.py 超时时会输出 "(超时未获取回答)" 且退出码为 0, 必须识别为失败并继续兜底
    if "超时未获取回答" in out or "未获取回答" in out:
        raise RuntimeError("deepseek-chat-cli 超时未获取回答")
    return out


def call_llm(cfg, api_key, system, user, dsc_prompt=None):
    """按顺序尝试 LLM provider; 本地模式(cfg.local_ollama)只走 Ollama qwen2.5:7b"""
    if cfg.get("local_ollama"):
        providers = [cfg.get("llm_ollama")]
    else:
        providers = [cfg.get("llm")]
        cli = cfg.get("llm_cli")
        if cli and cli.get("provider") != cfg.get("llm", {}).get("provider"):
            providers.append(cli)
        fallback = cfg.get("llm_fallback")
        if fallback and fallback.get("provider") != cfg.get("llm", {}).get("provider"):
            providers.append(fallback)
    NO_KEY_PROVIDERS = {"ollama"}
    last_err = ""
    for pcfg in providers:
        if not pcfg:
            continue
        provider = pcfg.get("provider", "opencode-go")
        key = load_llm_key(provider) or api_key
        if not key and provider not in NO_KEY_PROVIDERS:
            last_err = f"{provider}: 未找到 API key/token"
            log(f"⚠️ LLM {provider}: {last_err}, 跳过")
            continue
        try:
            if provider == "deepseek-chat-cli":
                log(f"调用 LLM {provider}...")
                out = _call_dsc(pcfg, key, dsc_prompt or f"{system}\n\n{user}")
            else:
                log(f"调用 LLM {provider} ({pcfg.get('model', '')})...")
                out = _call_llm_once(pcfg, key, system, user)
            # 任何 provider 返回超时/未获取回答都视为失败, 继续下一个兜底
            if "超时未获取回答" in out or "未获取回答" in out:
                raise RuntimeError(f"{provider} 返回超时未获取回答")
            return out
        except Exception as e:
            last_err = f"{provider}: {e}"
            log(f"❌ LLM {provider} 失败: {e}")
    raise RuntimeError(last_err or "没有可用的 LLM provider")


def load_history(cfg, days=5):
    """取最近 days 天的报告原文(截断), 供观点连续性对比"""
    rdir = os.path.join(WORKDIR, "reports")
    if not os.path.isdir(rdir):
        return ""
    files = sorted(glob.glob(os.path.join(rdir, "*.md")))
    if not files:
        return ""
    cutoff = datetime.datetime.now(CN_TZ) - datetime.timedelta(days=days)
    parts = []
    for f in files:
        try:
            d = datetime.datetime.strptime(os.path.basename(f)[:10], "%Y-%m-%d")
            if d.replace(tzinfo=CN_TZ) >= cutoff:
                parts.append(read_text(f))
        except ValueError:
            pass
    return "\n\n".join(parts)[-6000:]


ANALYZE_SYSTEM = """你是资深财经媒体分析助手, 专精解读A股著名"多头"李大霄的短视频内容。
请严格按照以下规则输出分析 (全部使用简体中文):

【输出模板】(必须严格按此结构, 用Markdown):
【一句话摘要】用一句话概括本期视频的核心结论
【核心观点】3-8条, 每条必须包含原文数据(点位/百分比/日期等), 格式: N. 观点 (原文数据: ...)
【关键数据清单】列出视频中出现的具体数据: 美债收益率/巴菲特指标/见顶时间表/指数点位/成交量等, 格式: - 数据项: 数值
【暗示提取】李大霄常说"不是推荐", 这实为合规话术下的真实关注点。请逐条列出并判断语境属于"机会暗示"或"风险警示", 每条标注: 🔴警示 / 🟢看多 / ⚪中性 / ⚠️风险
【市场定性】判断市场定性: 反弹/反转/见顶/调整/防御。注意他的核心框架: "没量=不是反转"。给出判断依据。
【操作含义】三层面: 方向(看多/看空/观望), 仓位(建议的仓位变化), 风险(需要警惕的风险点)
【观点连续性】与历史报告对比: 标注 延续/升级/新增/反转。如态度升级(如"警惕→高度警惕")必须重点标出。无历史报告则写"首次记录,无历史对比"。

最后必须另起一行输出: 以上为李大霄个人观点提炼,不构成投资建议"""


def clean_subtitle_for_prompt(subtitle_text):
    """去掉 [MM:SS] 时间戳和换行, 用中文逗号连接, 适合 C 端对话"""
    import re
    lines = []
    for ln in (subtitle_text or "").splitlines():
        s = re.sub(r"^\[\d{1,2}:\d{2}(?::\d{2})?\]\s*", "", ln).strip()
        if s:
            lines.append(s)
    return "，".join(lines)


def build_dsc_prompt(video, subtitle_text, history):
    """构造给 deepseek-chat-cli 的自然 C 端提示词(保持原输出格式)"""
    date = datetime.datetime.fromtimestamp(video.get("pubdate", 0), CN_TZ).strftime("%Y-%m-%d")
    clean_sub = clean_subtitle_for_prompt(subtitle_text)[:12000]
    return f"""你好，请以资深财经分析的角度，帮我分析一下李大霄这条视频的内容，并按下面的 Markdown 格式输出（全部使用简体中文）：

视频日期：{date}
视频标题：{video['title']}
视频时长：{video.get('dur_str', '')}

视频字幕（已去掉时间戳）：
{clean_sub}

最近 5 天的历史报告（供你对比观点是否延续/升级/新增/反转）：
{history or "(无历史报告)"}

请按以下格式输出：
【一句话摘要】用一句话概括本期视频的核心结论
【核心观点】3-8条, 每条必须包含原文数据(点位/百分比/日期等), 格式: N. 观点 (原文数据: ...)
【关键数据清单】列出视频中出现的具体数据: 美债收益率/巴菲特指标/见顶时间表/指数点位/成交量等, 格式: - 数据项: 数值
【暗示提取】李大霄常说"不是推荐", 这实为合规话术下的真实关注点。请逐条列出并判断语境属于"机会暗示"或"风险警示", 每条标注: 🔴警示 / 🟢看多 / ⚪中性 / ⚠️风险
【市场定性】判断市场定性: 反弹/反转/见顶/调整/防御。注意他的核心框架: "没量=不是反转"。给出判断依据。
【操作含义】三层面: 方向(看多/看空/观望), 仓位(建议的仓位变化), 风险(需要警惕的风险点)。其中方向、仓位、风险里的关键词（如看多/看空/观望、加仓/减仓、进攻/防御等）请用 ~关键词~ 这种波浪号标记标出（例如 ~防御~、~看空~、~减仓~），我会在后处理时转成加粗。
【观点连续性】与历史报告对比: 标注 延续/升级/新增/反转。如态度升级(如"警惕→高度警惕")必须重点标出。无历史报告则写"首次记录,无历史对比"。

最后必须另起一行输出：以上为李大霄个人观点提炼,不构成投资建议"""


def analyze_subtitles(cfg, api_key, video, subtitle_text, history):
    date = datetime.datetime.fromtimestamp(video.get("pubdate", 0), CN_TZ).strftime("%Y-%m-%d")
    user = f"""【视频信息】
日期: {date}
标题: {video['title']}
时长: {video.get('dur_str', '')}

【字幕文本】
{subtitle_text[:12000]}

【历史报告(最近5天, 供观点连续性对比)】
{history or "(无历史报告)"}

请按模板输出分析。"""
    sys_prompt = ANALYZE_SYSTEM
    dsc_prompt = build_dsc_prompt(video, subtitle_text, history)
    return call_llm(cfg, api_key, sys_prompt, user, dsc_prompt=dsc_prompt)


# =====================================================================
# 5. 每日报告
# =====================================================================
def report_path(date_str):
    return os.path.join(WORKDIR, "reports", f"{date_str}.md")


def parse_report(text):
    """解析报告 -> {summary_lines, list_lines, sections:{bvid:(start,end)}}"""
    res = {"summary_lines": [], "list_lines": [], "sections": {}}
    lines = text.splitlines()
    in_summary = in_list = False
    cur_bvid = None
    sec_start = None
    for i, ln in enumerate(lines):
        if ln.startswith("## 今日摘要"):
            in_summary, in_list = True, False
            continue
        if ln.startswith("## 视频清单"):
            in_summary, in_list = False, True
            continue
        m = re.match(r"^## 视频\d+:《(.+?)》\s*\(?(BV\w+)\)?\s*$", ln)
        if m:
            if cur_bvid and sec_start is not None:
                res["sections"][cur_bvid] = (sec_start, i)
            cur_bvid, sec_start = m.group(2), i
            in_summary = in_list = False
            continue
        if in_summary and ln.startswith("- "):
            res["summary_lines"].append(ln)
        elif in_list and ln.startswith("- "):
            res["list_lines"].append(ln)
    if cur_bvid and sec_start is not None:
        res["sections"][cur_bvid] = (sec_start, len(lines))
    return res


def upsert_report(video, section_md, summary_line, list_line):
    """追加/更新当日报告; 返回报告路径"""
    date_str = datetime.datetime.fromtimestamp(
        video.get("pubdate", time.time()), CN_TZ).strftime("%Y-%m-%d")
    rp = report_path(date_str)
    bvid = video["bvid"]

    if os.path.exists(rp):
        text = read_text(rp)
    else:
        text = (f"# 📺 李大霄视频日报 {date_str}\n\n"
                f"## 今日摘要\n\n"
                f"## 视频清单\n\n")

    # 视频清单行: 有则替换, 无则插入
    list_lines = [l for l in text.splitlines() if l.startswith("- ")]
    # 先删掉旧清单行 (基于BV匹配), 稍后统一重建
    lines = text.splitlines()
    new_lines = []
    in_list = False
    for ln in lines:
        if ln.startswith("## 视频清单"):
            in_list = True
            new_lines.append(ln)
            continue
        if in_list and ln.startswith("## "):
            in_list = False
        if in_list and ln.startswith("- ") and bvid in ln:
            continue
        new_lines.append(ln)
    text = "\n".join(new_lines)

    # 插入清单行
    text = re.sub(r"(## 视频清单\n)", r"\1" + list_line + "\n", text, count=1)

    # 摘要行: 删除旧的该视频摘要 (按BV或标题匹配), 插入到 今日摘要 区块
    lines = text.splitlines()
    new_lines = []
    in_summary = False
    for ln in lines:
        if ln.startswith("## 今日摘要"):
            in_summary = True
            new_lines.append(ln)
            continue
        if in_summary and ln.startswith("## "):
            in_summary = False
        if in_summary and ln.startswith("- ") and (bvid in ln or video["title"] in ln):
            continue
        new_lines.append(ln)
    text = "\n".join(new_lines)
    text = re.sub(r"(## 今日摘要\n)", r"\1" + summary_line + "\n", text, count=1)

    # 视频章节: 已有同BV章节则整体替换, 否则末尾追加
    sec = parse_report(text)["sections"].get(bvid)
    if sec is not None:
        s, e = sec
        lines = text.splitlines()
        text = "\n".join(lines[:s] + [section_md] + lines[e:])
        log(f"报告章节已更新(BV {bvid}): {rp}")
    else:
        num = len(re.findall(r"^## 视频\d+:", text, re.M)) + 1
        section_md = re.sub(r"^## 视频\d+:", f"## 视频{num}:", section_md, count=1)
        text = text.rstrip() + "\n\n" + section_md + "\n"
        log(f"报告已追加视频#{num}: {rp}")
    write_text(rp, text)
    return rp


def build_section(cfg, video, subtitle_text, analysis, err=None):
    """生成单个视频的报告章节 markdown"""
    head = f"## 视频1:《{video['title']}》 ({video['bvid']})"
    if err:
        return (head + f"\n\n> ⚠️ 处理失败: {err}\n", "")
    if not subtitle_text:
        return (head + "\n\n> 无字幕, 跳过分析\n", f"- 视频《{video['title']}》: 无字幕, 跳过分析")
    if analysis is None:
        return (head + "\n\n> ⚠️ AI分析失败, 仅保留字幕\n", "")
    one_line = ""
    m = re.search(r"【一句话摘要】\s*(.+)", analysis)
    if m:
        one_line = m.group(1).strip()
    summary_line = f"- 视频《{video['title']}》: {one_line}" if one_line else ""
    body = head + "\n\n" + analysis.strip() + "\n"
    return (body, summary_line)


def ensure_disclaimer(analysis):
    if DISCLAIMER not in analysis:
        analysis = analysis.rstrip() + "\n\n" + DISCLAIMER
    return analysis


def postprocess_analysis(analysis):
    """后处理 LLM 输出:
    1) ~关键词~ -> **关键词** (dsc 的波浪号加粗标记)
    2) 清洗 DeepSeek 网页端引用编号残留, 如 -4、-4-10、- 6 - 8 等
    """
    # 1) 波浪号加粗
    analysis = re.sub(r"~([^~\n]+)~", r"**\1**", analysis)
    # 2) 清洗引用编号: ")-4-10" -> ")" 以及行尾 "-4" / "- 6 - 8"
    analysis = re.sub(r"\)\s*-\s*\d+(?:\s*-\s*\d+)*", ")", analysis)
    analysis = re.sub(r"\s*-\s*\d+(?:\s*-\s*\d+)*\s*(?=[。\n]|$)", "", analysis)
    # 清理多余空白
    analysis = re.sub(r"[ \t]+", " ", analysis)
    return analysis.strip()


# =====================================================================
# 主流程
# =====================================================================
def process_video(cfg, api_key, video):
    """处理单个视频: 下载->字幕->分析->报告; 返回 (ok, report_path, note)"""
    bvid = video["bvid"]
    if not video.get("pubdate"):
        info = fetch_video_info(bvid)
        if info:
            video.update(info)
        else:
            video["pubdate"] = int(time.time())   # 兜底: 用当前时间
    log(f"---- 处理视频: {video.get('title', '')} ({bvid}) ----")

    # 下载
    mp4 = find_downloaded_mp4(bvid)
    if not mp4:
        mp4 = download_video(cfg, bvid)
    if not mp4:
        note = f"下载失败(重试后仍失败), 已跳过: {bvid}"
        log("❌ " + note)
        return (False, None, note)

    # 字幕
    sub_path = os.path.join(WORKDIR, "data", "subtitles", f"{bvid}.txt")
    subtitle_text = read_text(sub_path) if os.path.exists(sub_path) else ""
    if not subtitle_text:
        subtitle_text, stats = ocr_video(cfg, mp4, bvid)
        if not subtitle_text:
            log("无字幕, 跳过分析")
            section, sl = build_section(cfg, video, "", None, None)
            rp = upsert_report(video, section, sl, list_line(video))
            return (True, rp, "无字幕,跳过分析")

    # 分析
    history = load_history(cfg)
    analysis = None
    last_err = ""
    for attempt in range(1, cfg["llm_retries"] + 1):
        try:
            log(f"AI分析中 (第{attempt}次)...")
            analysis = analyze_subtitles(cfg, api_key, video, subtitle_text, history)
            # 防御: 任何 provider 返回超时/未获取回答都视为失败, 不写入报告
            if "超时未获取回答" in analysis or "未获取回答" in analysis:
                raise ValueError("LLM 返回超时未获取回答")
            analysis = ensure_disclaimer(analysis)
            analysis = postprocess_analysis(analysis)
            break
        except Exception as e:
            last_err = str(e)
            log(f"AI分析失败(第{attempt}次): {last_err}")
            time.sleep(5 * attempt)
    if analysis is None:
        note = f"AI分析失败: {last_err}"
        log("❌ " + note)
        section, sl = build_section(cfg, video, subtitle_text, None, None)
        section = section + f"\n> ⚠️ AI分析失败: {last_err}\n"
        rp = upsert_report(video, section, sl, list_line(video))
        return (False, rp, note)

    # 写报告
    section, summary_line = build_section(cfg, video, subtitle_text, analysis, None)
    rp = upsert_report(video, section, summary_line, list_line(video))
    log(f"✅ 视频处理完成: {bvid}")
    return (True, rp, "ok")


def list_line(video):
    t = datetime.datetime.fromtimestamp(video.get("pubdate", time.time()), CN_TZ)
    return f"- [{t.strftime('%H:%M')}] {video['title']} ({video['bvid']}) {video.get('dur_str', '')}"


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
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(keep) + "\n")


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


def main():
    ap = argparse.ArgumentParser(description="李大霄视频自动监控分析系统")
    ap.add_argument("--force", action="store_true",
                    help="强制重新处理最新视频(即使已处理过)")
    ap.add_argument("--uid", default=None, help="UP主UID")
    ap.add_argument("--limit", type=int, default=None, help="每次最多检查的视频数")
    ap.add_argument("--config", default=None, help="配置文件路径")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.uid:
        cfg["uid"] = args.uid
    if args.limit:
        cfg["limit"] = args.limit
    api_key = load_llm_key()
    if not cfg.get("local_ollama") and not api_key:
        log("❌ 未找到 LLM 密钥 (OPENCODE_GO_API_KEY / DEEPSEEK_API_KEY, 检查 .env 或环境变量)")
        return 1

    lock = RunLock(os.path.join(WORKDIR, "data", "monitor.lock"))
    if not lock.acquire():
        return 0
    try:
        # 1. 检测更新
        videos = get_space_videos(cfg, cfg["limit"])
        if not videos:
            log("❌ 无法获取UP主视频列表(风控或网络问题)")
            return 1
        # 只处理 baseline(前天)及之后发布的视频, 更早的忽略
        videos = [v for v in videos if is_from_baseline(v)]
        if not videos:
            log("前天以来暂无新视频, 退出")
            return 0
        latest = videos[0]
        last_file = os.path.join(STATE_DIR, "last_bvid.txt")
        last_bvid = read_text(last_file).strip()
        done = processed_bvids()
        status_map = processed_status_map()

        # 前天以来所有视频都已成功处理时直接退出; 有失败/部分成功/未处理则继续
        if not args.force and all(
            v["bvid"] in done and is_success_status(status_map.get(v["bvid"]))
            for v in videos
        ):
            log(f"无新视频 (前天以来均已处理), 退出")
            return 0

        # 2. 确定候选: force=仅最新; 首次运行=仅最新;
        #    常规=前天以来未处理/失败/部分成功的视频 (按时间从旧到新)
        if args.force:
            candidates = [latest]
            log("--force 模式: 强制处理最新视频")
        elif not last_bvid:
            candidates = [latest]
            log("首次运行, 处理最新视频")
        else:
            cand = [v for v in videos
                    if v["bvid"] not in done or not is_success_status(status_map.get(v["bvid"]))]
            candidates = list(reversed(cand))        # 旧的在前
            if not candidates:
                log("无新视频 (最新已处理), 退出")
                return 0
            log(f"发现 {len(candidates)} 个待处理视频")

        # 3. 逐个处理
        touched = []
        for v in candidates:
            ok, rp, note = process_video(cfg, api_key, v)
            status = "ok" if ok else ("error" if rp is None else "partial")
            if note == "无字幕,跳过分析":
                status = "no-subtitle"
            mark_processed(v["bvid"], v.get("title", ""), status)
            if rp and rp not in touched:
                touched.append(rp)
            log(f"状态: {v['bvid']} -> {status} ({note})")

        # 4. 更新状态文件
        write_text(last_file, latest["bvid"] + "\n")
        log(f"状态已更新: last_bvid={latest['bvid']}")

        # 5. 输出确认
        if touched:
            for rp in touched:
                print(f"✅ 今日报告: {rp}")
        return 0
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
