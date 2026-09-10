#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
李大霄视频自动监控分析系统 - 主脚本 (Windows)
====================================================
功能:
  1. 更新检测  : bilidown space 获取 UP主最新视频, 与 data/last_bvid.txt 对比
  2. 视频下载  : bilidown dl 下载 480P mp4 (失败重试1次)
  3. 字幕提取  : ffmpeg 抽帧 + RapidOCR 硬字幕识别 (自适应字幕区域 + 相邻帧去重)
  4. 片尾识别  : ffmpeg 取片尾帧 + deepseek-flash 视觉模型读"片尾暗示"
                (结束语/荐书卡/合规声明卡/数据面板; OCR 会丢弃的整屏密集文字)
  5. AI 分析   : OpenCode Zen Go API (deepseek-flash) 对字幕+片尾做"李大霄话术解码"分析
  6. 每日报告  : reports/YYYY-MM-DD.md (当天已有报告则追加)
  7. 状态幂等  : data/last_bvid.txt + data/processed.txt + 并发锁
                (片尾识别结果缓存于 state/ending_hints.json, 随仓库同步)

用法:
  python monitor.py            # 正常运行 (无新视频秒退)
  python monitor.py --force    # 强制重新处理最新视频
  python monitor.py --uid 2137589551 --limit 10
  python monitor.py --config config.json
  python monitor.py --check-vision   # 仅自检片尾视觉通道 (Actions 预检用)

退出码: 0=正常(含无更新)  1=出错
"""
import argparse
import base64
import datetime
import glob
import io
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
            "model": "deepseek-flash",
            "reasoningEffort": "max",           # 思考强度: low/high/max (旧值 minimal/medium/xhigh/ultra 会被官方映射)
            "temperature": 0.3,
            "max_tokens": 8000,
        },
        "llm_fallback": {                       # OpenCode Go 失败时的 DeepSeek 兜底
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com",
            # 官方最新模型名: deepseek-flash (DeepSeek-V4.1-Flash)。
            # 旧名 deepseek-chat / deepseek-v4-flash 已下线, 请求会被路由到 V4.1 Flash。
            "model": "deepseek-flash",
            "reasoningEffort": "medium",        # 思考强度: low/high/max (旧名 medium 会被抬到 high)
            "temperature": 0.3,                 # 思考模式下官方 API 会忽略该参数
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
        "vision": {                             # 片尾画面识别 (deepseek-flash 视觉能力)
            "enabled": True,
            "provider": "opencode-go",
            "base_url": "https://opencode.ai/zen/go/v1",
            "model": "deepseek-flash",          # 官方唯一支持图像理解的模型
            "max_tokens": 6000,                 # 思考模式下需留足思考token, 否则返回空
            "timeout": 240,
            "tail_seconds": 45,                 # 片尾扫描窗口(秒)
            "max_frames": 6,                    # 送模型的最大帧数(覆盖不同场景)
            "max_width": 1280,                  # 帧降采样宽度(省token)
            "jpeg_quality": 85,
        },
        "vision_fallback": {                    # 视觉兜底: DeepSeek 官方 API (同一模型)
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-flash",
            "max_tokens": 6000,
            "timeout": 240,
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
        cfg["vision"].update(user.get("vision", {}))
        cfg["vision_fallback"].update(user.get("vision_fallback", {}))
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
            v["duration"] = info.get("duration", 0)
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


def expected_duration(video):
    """期望时长(秒): B站 API duration 优先, 其次解析 dur_str ("06:21")"""
    d = float(video.get("duration") or 0)
    if d > 0:
        return d
    parts = []
    for x in (video.get("dur_str") or "").split(":"):
        x = x.strip()
        if x.isdigit():
            parts.append(int(x))
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return 0.0


def ensure_complete_download(cfg, video, mp4, tol=0.97):
    """下载完整性校验: 实际时长明显短于B站元数据 -> 重下一次。

    截断的 mp4(容器头仍报完整时长)会让 ffmpeg 提前结束, 抽帧数变少,
    直接后果是字幕不全、片尾窗口落在视频中间(读不到真正的片尾)。
    返回 (mp4, warn): warn 非空表示最终仍残缺, 需要写进报告提醒。
    """
    if not mp4 or not os.path.exists(mp4):
        return mp4, ""
    exp = expected_duration(video)
    got = ffprobe_duration(mp4)
    if not exp or not got or got >= exp * tol:
        return mp4, ""

    log(f"⚠️ 下载疑似不完整: 实际 {got:.0f}s / 预期 {exp:.0f}s "
        f"({got / exp * 100:.0f}%) -> 重新下载")
    bad = mp4 + ".partial"
    try:
        os.replace(mp4, bad)          # 挪走, 让 download_video 重新下载
    except Exception as e:
        warn = f"视频下载不完整(实际 {got:.0f}s / 预期 {exp:.0f}s), 内容可能缺失"
        log(f"⚠️ 无法移走残缺文件({e}), 不再重下; {warn}")
        return mp4, warn

    again = download_video(cfg, video["bvid"])
    got2 = ffprobe_duration(again) if again else 0.0
    if again and got2 > got:
        try:
            os.remove(bad)
        except Exception:
            pass
        if got2 >= exp * tol:
            log(f"✅ 重新下载完整: {got2:.0f}s / 预期 {exp:.0f}s")
            return again, ""
        warn = f"视频下载仍不完整(实际 {got2:.0f}s / 预期 {exp:.0f}s), 内容可能缺失"
        log("⚠️ " + warn)
        return again, warn

    # 重下反而更短/失败 -> 恢复原文件
    if again:
        try:
            os.remove(again)
        except Exception:
            pass
    if os.path.exists(bad):
        try:
            os.replace(bad, mp4)
        except Exception:
            pass
    warn = f"视频下载不完整(实际 {got:.0f}s / 预期 {exp:.0f}s), 内容可能缺失"
    log("⚠️ " + warn)
    return mp4, warn


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
# 3.5 片尾画面识别 (deepseek-flash 视觉能力)
#     李大霄视频片尾固定是"卡片区": 口播结束语 + 荐书卡 + 合规声明卡(+偶尔数据面板),
#     这类整屏密集文字会被 OCR 的字数/数字过滤规则丢弃, 因此单独用视觉模型读片尾。
# =====================================================================
ENDING_PROMPT_VER = 3          # 提示词版本: 升版后旧缓存自动失效重跑
ENDING_PROMPT = """这是同一条财经短视频"片尾"(最后几十秒)的连续画面, 按时间顺序给出, 已去重。
请识别片尾画面里的**文字与图形**, 严格按下面 6 行输出(简体中文, 每行一条, 不要解释、不要逐帧罗列、相同内容只写一次):

【片尾口语】片尾画面中的口播字幕/结束语原文(多条用" / "连接; 没有就写"无")
【片尾卡片】片尾固定卡片: 类型(声明/荐书/数据面板/其他)+关键文字, 多张卡片用" ; "分隔, 每张不超过40字
【片尾画面】片尾出现的图形/动画/插图: 地球/星球/球体、钻石、婴儿、山峰/山谷、人物、箭头、图标、图表、二维码等, 写清形态与大致位置(如"左侧蓝色地球带云层, 前景人物侧脸"); 没有图形就写"无"
【片尾画面暗示】只依据**图形/动画**(不看文字)判断画面传递的信号。这是本任务最重要的一行, 必须单独完成, 不要与口播/卡片混在一起。李大霄常用标志性图形隐喻, 逐一对照:
  地球/星球/球体/地球仪 → "地球顶"·高位见顶风险
  钻石/宝石 → "钻石底"·低位机会
  婴儿/儿童 → "婴儿底"·底部区域
  山峰/山峦/登顶 → 顶部/高位; 山谷/深谷/下坡 → 底部/低位
  向上箭头 → 看多/上涨; 向下箭头 → 看空/下跌
  其他图形(书/二维码/图标等)不构成行情隐喻, 不要附会。
  输出格式: 先写看到的形态, 再判定含义, 最后给方向标记:
    "形态 → 含义 → 🔴高位风险" 或 "形态 → 含义 → 🟢低位机会" 或 "形态 → 含义 → ⚠️方向冲突(同时出现顶部与底部图形)"
  画面里**确实出现**了上述任一隐喻图形时, 要用肯定语气直接判定(如"出现地球/星球, 即'地球顶', 高位风险提示"), 不要写"可能对应"; 没有出现就整行写"无"。绝对不要编造画面里没有的图形。
【片尾数据】片尾画面中的具体数据/标的/日期(没有就写"无")
【片尾暗示】用1-2句综合判断片尾传递的信号, 并点明画面隐喻与文字(口播/卡片)是否一致

要求: 只描述画面里真实存在的内容(文字或图形), 不要臆造; 全文不超过500字。"""


def _ending_cache_path():
    return os.path.join(STATE_DIR, "ending_hints.json")


def load_ending_cache():
    p = _ending_cache_path()
    if not os.path.exists(p):
        return {}
    try:
        return json.loads(read_text(p) or "{}")
    except Exception:
        return {}


def save_ending_cache(cache):
    os.makedirs(STATE_DIR, exist_ok=True)
    write_text(_ending_cache_path(),
               json.dumps(cache, ensure_ascii=False, indent=1, sort_keys=True) + "\n")


def tail_frames(cfg, video_path, bvid):
    """取片尾候选帧: 优先复用 OCR 已抽的帧(零成本), 否则用 ffmpeg 只抽片尾"""
    vcfg = cfg.get("vision") or {}
    tail_s = float(vcfg.get("tail_seconds", 45) or 45)
    dur = ffprobe_duration(video_path) if video_path else 0.0
    interval = cfg.get("frame_interval") or max(1.0, min(4.0, (dur or 240) / 160.0))
    want = int(max(6, min(40, tail_s / max(0.5, interval) + 2)))

    fdir = os.path.join(WORKDIR, "data", "frames", bvid)
    frames = sorted(glob.glob(os.path.join(fdir, "*.jpg")))
    if frames:
        return frames[-want:]

    if not video_path or not os.path.exists(video_path):
        return []
    edir = os.path.join(WORKDIR, "data", "ending_frames", bvid)
    cached = sorted(glob.glob(os.path.join(edir, "*.jpg")))
    if cached:
        return cached[-want:]
    os.makedirs(edir, exist_ok=True)
    pat = os.path.join(edir, "t_%03d.jpg")
    cmd = [FFMPEG, "-y", "-v", "error", "-sseof", f"-{tail_s:.0f}",
           "-i", video_path, "-vf", "fps=1", "-q:v", "3", pat]
    log(f"片尾抽帧: 最后 {tail_s:.0f}s -> {edir}")
    try:
        subprocess.run(cmd, check=True, timeout=600, capture_output=True, text=True)
    except Exception as e:
        log(f"⚠️ 片尾抽帧失败(-sseof): {e}")
        # 兜底: 全片 1fps 抽帧后取尾部 (短视频/异常容器时 -sseof 可能不可用)
        try:
            subprocess.run([FFMPEG, "-y", "-v", "error", "-i", video_path,
                            "-vf", "fps=1", "-q:v", "3", pat],
                           check=True, timeout=1800, capture_output=True, text=True)
        except Exception as e2:
            log(f"⚠️ 片尾抽帧失败(全片兜底): {e2}")
            return []
    return sorted(glob.glob(os.path.join(edir, "*.jpg")))[-want:]


def frame_sig(path, size=16):
    """16x16 灰度签名: 比 64bit 平均哈希更能区分"深色背景+不同画面"的片尾卡片"""
    from PIL import Image
    with Image.open(path) as im:
        return list(im.convert("L").resize((size, size)).getdata())


def _sig_mad(a, b):
    """两张图签名的平均绝对差 (0~255)"""
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def dedupe_frames(paths, max_keep=6, dup_th=3.0):
    """片尾选帧 (两阶段):

    1) 相邻近似帧合并: 同一张卡片/同一句字幕只留一帧
    2) 最远点采样: 在剩余帧里反复挑"与已选帧差异最大"的, 覆盖不同画面
       (口播结束语 / 荐书卡 / 地球动画 / 声明卡 是不同场景, 均匀取样会整段漏掉)
    必含最后一帧所在场景(片尾声明卡)。
    """
    if not paths:
        return []
    kept = [paths[0]]
    sigs = {paths[0]: frame_sig(paths[0])}
    for p in paths[1:]:
        s = frame_sig(p)
        sigs[p] = s
        if _sig_mad(s, sigs[kept[-1]]) >= dup_th:
            kept.append(p)

    picks = [kept[-1]]                       # 从最后一帧(声明卡)开始
    while len(picks) < max_keep and len(picks) < len(kept):
        best, best_d = None, -1.0
        for p in kept:
            if p in picks:
                continue
            d = min(_sig_mad(sigs[p], sigs[q]) for q in picks)
            if d > best_d:
                best, best_d = p, d
        if best is None or best_d <= 0:
            break
        picks.append(best)
    return sorted(picks)


def frame_data_url(path, max_width=1280, quality=85):
    """帧 -> data:image/jpeg;base64 (降采样省 token)"""
    from PIL import Image
    with Image.open(path) as im:
        im = im.convert("RGB")
        if max_width and im.width > max_width:
            h = max(1, round(im.height * max_width / im.width))
            im = im.resize((max_width, h), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=quality)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def vision_providers(cfg):
    """视觉 provider 优先级(固定): opencode-go -> DeepSeek 官方 API

    识图不走 deepseek-chat-cli (网页版 CLI 不支持图片输入)。
    """
    out = []
    for k in ("vision", "vision_fallback"):
        p = cfg.get(k)
        if not p:
            continue
        sig = (p.get("provider"), p.get("base_url"), p.get("model"))
        if any((q.get("provider"), q.get("base_url"), q.get("model")) == sig for q in out):
            continue
        out.append(p)
    return out


def _call_vision_once(pcfg, api_key, prompt, images):
    """向一个视觉端点发多图请求, 返回文本"""
    import requests
    _warn_model(pcfg)
    url = pcfg["base_url"].rstrip("/") + "/chat/completions"
    content = [{"type": "text", "text": prompt}]
    for p in images:
        content.append({"type": "image_url", "image_url": {
            "url": frame_data_url(p, pcfg.get("max_width", 1280), pcfg.get("jpeg_quality", 85))}})
    base = {"model": pcfg["model"], "messages": [{"role": "user", "content": content}],
            "stream": False}
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    def post(payload):
        r = requests.post(url, json=payload, headers=headers,
                          timeout=pcfg.get("timeout", 240))
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

    payload = dict(base, max_tokens=pcfg.get("max_tokens", 6000))
    if pcfg.get("temperature") is not None:
        payload["temperature"] = pcfg["temperature"]
    # 思考模式参数统一由 _apply_thinking 决定 (官方: thinking + reasoning_effort)
    _apply_thinking(payload, pcfg)
    j = post(payload)
    ch = (j.get("choices") or [{}])[0]
    text = (ch.get("message", {}).get("content") or "").strip()
    if not text:
        # deepseek-flash 默认开启思考: 思考 token 吃满 max_tokens 时会返回空内容
        # -> 显式关闭思考重试一次
        log("⚠️ 视觉模型返回空内容(思考token可能耗尽), 关闭思考重试...")
        try:
            j2 = post(dict(base, max_tokens=2000, thinking={"type": "disabled"}))
            text = ((j2.get("choices") or [{}])[0].get("message", {}).get("content") or "").strip()
        except Exception as e:
            raise RuntimeError(f"视觉模型返回空内容, 关思考重试失败: {e}")
        if not text:
            raise RuntimeError("视觉模型返回空内容")
    return text


def read_ending_hint(cfg, video, video_path=None, force=False):
    """读取片尾暗示: 抽片尾帧 -> 视觉模型识别 -> 缓存到 state/ending_hints.json

    返回 dict(provider/model/frames/text/ts) 或 None(未启用/无帧/全部失败)。
    任何失败都不抛异常, 不影响主流程。
    """
    vcfg = cfg.get("vision") or {}
    if not vcfg.get("enabled", True):
        return None
    bvid = video["bvid"]
    cache = load_ending_cache()
    hit = cache.get(bvid) or {}
    if not force and hit.get("text") and hit.get("ver") == ENDING_PROMPT_VER:
        log(f"片尾识别命中缓存: {bvid}")
        return hit

    frames = tail_frames(cfg, video_path, bvid)
    if not frames:
        log("片尾识别: 无可用片尾帧, 跳过")
        return None
    picks = dedupe_frames(frames, int(vcfg.get("max_frames", 6) or 6))
    log(f"片尾识别: 候选 {len(frames)} 帧 -> 去重后送 {len(picks)} 帧 "
        f"({', '.join(os.path.basename(x) for x in picks)})")

    last_err = ""
    for pcfg in vision_providers(cfg):
        provider = pcfg.get("provider", "")
        key = load_llm_key(provider)
        if not key:
            last_err = f"{provider}: 未找到 API key"
            log(f"⚠️ 片尾识别 {provider}: 无密钥, 跳过")
            continue
        try:
            log(f"片尾视觉识别: {provider} ({pcfg.get('model', '')})...")
            t0 = time.time()
            text = _call_vision_once(pcfg, key, ENDING_PROMPT, picks)
            rec = {"bvid": bvid, "status": "ok", "ver": ENDING_PROMPT_VER,
                   "provider": provider, "model": pcfg.get("model", ""),
                   "frames": len(picks), "text": text,
                   "ts": datetime.datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")}
            cache[bvid] = rec
            save_ending_cache(cache)
            log(f"✅ 片尾识别完成 ({provider}, {time.time() - t0:.1f}s, {len(text)}字)")
            return rec
        except Exception as e:
            last_err = f"{provider}: {e}"
            log(f"❌ 片尾识别 {provider} 失败: {e}")
    log(f"⚠️ 片尾识别全部失败: {last_err}")
    return None


def ending_md(rec):
    """片尾识别结果 -> 报告 markdown 块 (无结果返回空串)

    画面图形隐喻(【片尾画面暗示】)单独提出来置顶加粗:
    它是李大霄"地球顶/钻石底/婴儿底"这类标志性比喻的唯一来源, 容易被淹没在文字卡片里。
    """
    if not rec or not (rec.get("text") or "").strip():
        return ""
    model = rec.get("model", "")
    text = rec["text"].strip()
    lines = [ln.strip() for ln in text.splitlines()]

    metaphor = ""
    rest = []
    for ln in lines:
        if not ln:
            continue
        if ln.startswith("【片尾画面暗示】"):
            metaphor = ln.replace("【片尾画面暗示】", "").strip()
            continue
        rest.append(ln)

    out = [f"### 📽 片尾画面识别 (视觉: {model})", ""]
    # "无" 或空 表示没有标志性图形, 不额外占据显眼位置
    if metaphor and metaphor not in ("无", "none", "None"):
        out.append(f"**🎯 画面图形暗示: {metaphor}**")
        out.append("")
    out.append("\n".join(rest))
    return "\n".join(out) + "\n"


def check_vision(cfg):
    """预检视觉通道: 打印每个 provider 的可用性 (Actions 里非阻断自检)"""
    vcfg = cfg.get("vision") or {}
    if not vcfg.get("enabled", True):
        log("片尾视觉已禁用 (config.json vision.enabled=false), 跳过预检")
        return True
    ok_any = False
    frames = []
    for d in sorted(glob.glob(os.path.join(WORKDIR, "data", "frames", "*"))):
        fs = sorted(glob.glob(os.path.join(d, "*.jpg")))
        if fs:
            frames = fs[-1:]
            break
    if not frames:
        # 无现成帧时用一张合成图自检 (640x360, 足够大以通过格式校验)
        try:
            from PIL import Image, ImageDraw
            tmp = os.path.join(WORKDIR, "data", "vision_preflight.png")
            os.makedirs(os.path.dirname(tmp), exist_ok=True)
            im = Image.new("RGB", (640, 360), "white")
            ImageDraw.Draw(im).text((40, 160), "VISION CHECK 2026", fill="black")
            im.save(tmp)
            frames = [tmp]
        except Exception as e:
            log(f"❌ 无法生成预检图: {e}")
            return False
    for pcfg in vision_providers(cfg):
        provider = pcfg.get("provider", "")
        key = load_llm_key(provider)
        if not key:
            log(f"⏭ 视觉预检 {provider}: 无密钥")
            continue
        try:
            t0 = time.time()
            text = _call_vision_once(pcfg, key, "这张图里有什么文字? 直接回答, 不要解释。",
                                     frames[:1])
            log(f"✅ 视觉预检 {provider} ({pcfg.get('model', '')}) 可用 "
                f"{time.time() - t0:.1f}s: {text[:60]!r}")
            ok_any = True
        except Exception as e:
            log(f"❌ 视觉预检 {provider} ({pcfg.get('model', '')}) 不可用: {e}")
    if not ok_any:
        log("⚠️ 没有可用的视觉 provider (片尾识别将跳过, 主流程不受影响)")
    return ok_any


# =====================================================================
# 4. AI 分析
# =====================================================================
def _call_ollama_native(pcfg, system, user):
    """Ollama 走原生 /api/chat, 确保 num_gpu/ngl 等参数真正生效"""
    import requests
    base = pcfg["base_url"].rstrip("/")
    if base.endswith("/v1"):
        api_url = base[:-3] + "/api/chat"
    else:
        api_url = base + "/api/chat"
    options = {}
    option_map = {
        "n_cpu_moe": "num_cpu_moe",
        "num_batch": "num_batch",
        "num_ubatch": "num_ubatch",
        "cache_ram": "cache_ram",
    }
    for cfg_key, api_key in option_map.items():
        val = pcfg.get(cfg_key)
        if val is None or val == "":
            continue
        # 兼容 "30" 这种字符串数字, Ollama 要求整数
        try:
            val = int(val)
        except (TypeError, ValueError):
            pass
        options[api_key] = val
    options["num_predict"] = pcfg.get("max_tokens", 4000)
    options["temperature"] = pcfg.get("temperature", 0.3)
    payload = {
        "model": pcfg["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "options": options,
    }
    r = requests.post(api_url, json=payload, timeout=pcfg.get("timeout", 180))
    try:
        r.raise_for_status()
    except requests.HTTPError:
        raise RuntimeError(f"Ollama API {r.status_code}: {r.text[:500]}")
    j = r.json()
    return (j.get("message", {}).get("content") or "").strip()


# 官方已下线的模型名 -> 现行模型名 (旧名仍可调用, 但请求由 V4.1 Flash 承接)
RETIRED_MODEL_MAP = {
    "deepseek-chat": "deepseek-flash",
    "deepseek-reasoner": "deepseek-flash",
    "deepseek-v4-flash": "deepseek-flash",
    "deepseek-v4-flash-vision-exp": "deepseek-flash",
    "deepseek-v4-flash-free": "deepseek-flash",
}
# 思考强度取值; 官方另接受 minimal/medium/xhigh/ultra (映射到 low/high/max)
THINKING_EFFORTS = ("low", "high", "max")


def _warn_model(pcfg):
    """旧模型名提醒(不阻断): 官方已下线这些名字, 请求会被路由到 V4.1 Flash"""
    m = (pcfg.get("model") or "").strip()
    if m in RETIRED_MODEL_MAP:
        log(f"⚠️ 模型名 `{m}` 已下线, 建议改为 `{RETIRED_MODEL_MAP[m]}` (--config 或 config.json)")


def _apply_thinking(payload, pcfg):
    """思考模式参数 (官方文档: OpenAI 格式 {"thinking":{"type":"enabled/disabled"}}
    + {"reasoning_effort":"low/high/max"})。

    只有配了 reasoningEffort 才显式打开思考(并省略 temperature —— 思考模式下
    官方 API 不支持 temperature, 传了也不生效)。没配时原样保留, 保证
    OpenCode Zen Go / Ollama 等旧通道行为完全不变。
    """
    effort = (pcfg.get("reasoningEffort") or "").strip()
    if not effort:
        return payload
    if effort in ("off", "none", "disabled", "false"):
        payload["thinking"] = {"type": "disabled"}
        payload.setdefault("temperature", pcfg.get("temperature", 0.3))
        return payload
    if effort not in THINKING_EFFORTS:
        # 旧值兼容: minimal/medium/xhigh/ultra 官方会自动映射, 这里仅提示
        log(f"ℹ️ reasoningEffort=`{effort}` 非官方新取值 {THINKING_EFFORTS}, 官方会自动映射")
    payload["thinking"] = {"type": "enabled"}
    payload["reasoning_effort"] = effort
    payload.pop("temperature", None)
    return payload


def _call_llm_once(pcfg, api_key, system, user):
    """向单个 LLM provider 发起一次 Chat Completions 请求"""
    if pcfg.get("provider") == "ollama":
        return _call_ollama_native(pcfg, system, user)
    import requests
    _warn_model(pcfg)
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
    # 先按普通模式放 temperature, 再由思考模式参数决定是否移除
    payload["temperature"] = pcfg.get("temperature", 0.3)
    _apply_thinking(payload, pcfg)
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
    """按顺序尝试 LLM provider; 本地模式(cfg.local_ollama)只走 Ollama qwen2.5:7b

    文本优先级(固定): opencode-go -> deepseek-chat-cli -> DeepSeek 官方 API
    (即 cfg.llm -> cfg.llm_cli -> cfg.llm_fallback, 顺序由 providers 列表决定)
    """
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
  若输入附带了【片尾画面(视觉识别)】, 请额外补**两条**独立条目(都放在末尾):
    (1) 以"片尾:"开头 —— 说明片尾结束语/卡片(文字部分)透露的信号;
    (2) 以"片尾图形:"开头 —— 单独提取【片尾画面暗示】里给出的**画面图形隐喻**(如地球/星球=地球顶、钻石=钻石底、婴儿=婴儿底、山峰=顶部、山谷=底部),
        写成"片尾图形: 形态 → 含义 → 方向", 方向标记沿用 🔴高位风险 / 🟢低位机会; 若该行为"无"则写"片尾图形: 无标志性图形隐喻"。
    并必须在【操作含义】中体现画面隐喻带来的方向倾向。
【市场定性】判断市场定性: 反弹/反转/见顶/调整/防御。注意他的核心框架: "没量=不是反转"。给出判断依据。
【操作含义】三层面: 方向(看多/看空/观望), 仓位(建议的仓位变化), 风险(需要警惕的风险点)
【观点连续性】与历史报告对比: 标注 延续/升级/新增/反转。如态度升级(如"警惕→高度警惕")必须重点标出。无历史报告则写"首次记录,无历史对比"。
若输入附带了【片尾画面(视觉识别)】, 必须把它纳入判断: 在【暗示提取】末尾补**两条**条目 —— 一条以"片尾:"开头(结束语/卡片等文字信号), 一条以"片尾图形:"开头(画面图形隐喻, 取【片尾画面暗示】那行, 按"形态 → 含义 → 方向"写, 方向标记沿用 🔴高位风险 / 🟢低位机会; 无图形则写"无标志性图形隐喻"); 并在【操作含义】中体现画面隐喻的方向倾向。

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


def build_dsc_prompt(video, subtitle_text, history, ending=None):
    """构造给 deepseek-chat-cli 的自然 C 端提示词(保持原输出格式)"""
    date = datetime.datetime.fromtimestamp(video.get("pubdate", 0), CN_TZ).strftime("%Y-%m-%d")
    clean_sub = clean_subtitle_for_prompt(subtitle_text)[:12000]
    ending_block = ""
    if ending and (ending.get("text") or "").strip():
        ending_block = ("\n视频片尾画面(视觉识别, 视频最后几十秒的结束语/卡片):\n"
                        f"{ending['text'].strip()[:1500]}\n")
    return f"""你好，请以资深财经分析的角度，帮我分析一下李大霄这条视频的内容，并按下面的 Markdown 格式输出（全部使用简体中文）：

视频日期：{date}
视频标题：{video['title']}
视频时长：{video.get('dur_str', '')}

视频字幕（已去掉时间戳）：
{clean_sub}
{ending_block}
最近 5 天的历史报告（供你对比观点是否延续/升级/新增/反转）：
{history or "(无历史报告)"}

请按以下格式输出：
【一句话摘要】用一句话概括本期视频的核心结论
【核心观点】3-8条, 每条必须包含原文数据(点位/百分比/日期等), 格式: N. 观点 (原文数据: ...)
【关键数据清单】列出视频中出现的具体数据: 美债收益率/巴菲特指标/见顶时间表/指数点位/成交量等, 格式: - 数据项: 数值
【暗示提取】李大霄常说"不是推荐", 这实为合规话术下的真实关注点。请逐条列出并判断语境属于"机会暗示"或"风险警示", 每条标注: 🔴警示 / 🟢看多 / ⚪中性 / ⚠️风险; 若上面给了片尾画面, 末尾按顺序补两条: 一条以"片尾:"开头(文字/卡片信号), 一条以"片尾图形:"开头(画面图形隐喻, 按"形态 → 含义 → 方向"写, 方向用 🔴高位风险 / 🟢低位机会)
【市场定性】判断市场定性: 反弹/反转/见顶/调整/防御。注意他的核心框架: "没量=不是反转"。给出判断依据。
【操作含义】三层面: 方向(看多/看空/观望), 仓位(建议的仓位变化), 风险(需要警惕的风险点)。其中方向、仓位、风险里的关键词（如看多/看空/观望、加仓/减仓、进攻/防御等）请用 ~关键词~ 这种波浪号标记标出（例如 ~防御~、~看空~、~减仓~），我会在后处理时转成加粗。
【观点连续性】与历史报告对比: 标注 延续/升级/新增/反转。如态度升级(如"警惕→高度警惕")必须重点标出。无历史报告则写"首次记录,无历史对比"。

最后必须另起一行输出：以上为李大霄个人观点提炼,不构成投资建议"""


def analyze_subtitles(cfg, api_key, video, subtitle_text, history, ending=None):
    date = datetime.datetime.fromtimestamp(video.get("pubdate", 0), CN_TZ).strftime("%Y-%m-%d")
    ending_block = ""
    if ending and (ending.get("text") or "").strip():
        ending_block = ("\n【片尾画面(视觉识别, 视频最后几十秒)】\n"
                        f"{ending['text'].strip()[:2000]}\n")
    user = f"""【视频信息】
日期: {date}
标题: {video['title']}
时长: {video.get('dur_str', '')}

【字幕文本】
{subtitle_text[:12000]}
{ending_block}
【历史报告(最近5天, 供观点连续性对比)】
{history or "(无历史报告)"}

请按模板输出分析。"""
    sys_prompt = ANALYZE_SYSTEM
    dsc_prompt = build_dsc_prompt(video, subtitle_text, history, ending=ending)
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


def build_section(cfg, video, subtitle_text, analysis, err=None, ending=None, warn=None):
    """生成单个视频的报告章节 markdown"""
    head = f"## 视频1:《{video['title']}》 ({video['bvid']})"
    if warn:
        head += f"\n\n> ⚠️ {warn}"
    emd = ending_md(ending).strip()

    def with_tail(text):
        """片尾块插在免责声明之前, 保证"以上为...不构成投资建议"仍是章节最后一行"""
        if not emd:
            return text + "\n"
        if DISCLAIMER in text:
            i = text.rfind(DISCLAIMER)
            return (text[:i].rstrip() + "\n\n" + emd + "\n\n"
                    + text[i:].strip() + "\n")
        return text + "\n\n" + emd + "\n"

    if err:
        return (with_tail(head + f"\n\n> ⚠️ 处理失败: {err}"), "")
    if not subtitle_text:
        note = "无字幕, 跳过分析" + ("; 已单独识别片尾画面" if emd else "")
        return (with_tail(head + f"\n\n> {note}"),
                f"- 视频《{video['title']}》: {note}")
    if analysis is None:
        return (with_tail(head + "\n\n> ⚠️ AI分析失败, 仅保留字幕"), "")
    one_line = ""
    m = re.search(r"【一句话摘要】\s*(.+)", analysis)
    if m:
        one_line = m.group(1).strip()
    summary_line = f"- 视频《{video['title']}》: {one_line}" if one_line else ""
    return (with_tail(head + "\n\n" + analysis.strip()), summary_line)


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
    mp4, dl_warn = ensure_complete_download(cfg, video, mp4)
    if not mp4:
        note = f"下载失败(重试后仍失败), 已跳过: {bvid}"
        log("❌ " + note)
        return (False, None, note)

    # 字幕
    sub_path = os.path.join(WORKDIR, "data", "subtitles", f"{bvid}.txt")
    subtitle_text = read_text(sub_path) if os.path.exists(sub_path) else ""
    if not subtitle_text:
        subtitle_text, stats = ocr_video(cfg, mp4, bvid)

    # 片尾画面识别 (deepseek-flash 视觉; 放在抽帧之后可直接复用帧; 失败不影响主流程)
    ending = read_ending_hint(cfg, video, mp4)

    if not subtitle_text:
        log("无字幕, 跳过分析")
        section, sl = build_section(cfg, video, "", None, None, ending=ending, warn=dl_warn)
        rp = upsert_report(video, section, sl, list_line(video))
        return (True, rp, "无字幕,跳过分析")

    # 分析
    history = load_history(cfg)
    analysis = None
    last_err = ""
    for attempt in range(1, cfg["llm_retries"] + 1):
        try:
            log(f"AI分析中 (第{attempt}次)...")
            analysis = analyze_subtitles(cfg, api_key, video, subtitle_text, history,
                                         ending=ending)
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
        section, sl = build_section(cfg, video, subtitle_text, None, None,
                                    ending=ending, warn=dl_warn)
        section = section + f"\n> ⚠️ AI分析失败: {last_err}\n"
        rp = upsert_report(video, section, sl, list_line(video))
        return (False, rp, note)

    # 写报告
    section, summary_line = build_section(cfg, video, subtitle_text, analysis, None,
                                          ending=ending, warn=dl_warn)
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
    ap.add_argument("--check-vision", action="store_true",
                    help="仅自检片尾视觉模型可用性后退出(不下载/不分析)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.check_vision:
        return 0 if check_vision(cfg) else 1
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
