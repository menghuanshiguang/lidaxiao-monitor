#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
李大霄视频自动监控分析系统 - 主脚本 (Windows)
====================================================
功能:
  1. 更新检测  : bilidown space 获取 UP主最新视频, 与 data/last_bvid.txt 对比
  2. 视频下载  : bilidown dl 下载 480P mp4 (失败重试1次)
  3. 字幕提取  : ffmpeg 抽帧 + RapidOCR 硬字幕识别 (自适应字幕区域 + 相邻帧去重)
  4. 片尾识别  : ffmpeg 取片尾帧 + 视觉链(MiMo免密 → opencode-go → deepseek-vision-cli 网页版识图 → DeepSeek官方)
                读"片尾暗示" (结束语/荐书卡/合规声明卡/数据面板; OCR 会丢弃的整屏密集文字)
  5. AI 分析   : OpenCode Zen Go API (deepseek-flash) 对字幕+片尾做"李大霄话术解码"分析
  6. 每日报告  : reports/YYYY-MM-DD.md (当天已有报告则追加)
  7. 状态幂等  : data/last_bvid.txt + data/processed.txt + 并发锁
                (片尾识别结果缓存于 state/ending_hints.json, 随仓库同步)

用法:
  python monitor.py            # 正常运行 (无新视频秒退)
  python monitor.py --force    # 强制重新处理最新视频
  python monitor.py --uid 2137589551 --limit 10
  python monitor.py --config config.json
  python monitor.py --check-vision   # 仅自检片尾视觉通道 (手动自检用)

退出码: 0=正常(含无更新)  1=出错
"""
import argparse
import base64
import datetime
import glob
import hashlib
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
from common import (  # noqa: E402
    CN_TZ, DISCLAIMER, STATE_DIR, WORKDIR)

try:
    import audit_p2 as _audit
except Exception:  # pragma: no cover - 模块缺失时不影响主流程
    _audit = None
# bash 路径可用环境变量 BILIDOWN_BASH 覆盖 (如 WSL/GitBash 安装在不同位置)
BASH = os.environ.get("BILIDOWN_BASH", r"D:\Program Files\Git\bin\bash.exe")
BILIDOWN_SCRIPT = os.path.join(WORKDIR, "_repo", "bin", "bilidown")
from common import (  # noqa: E402
    CN_TZ, FFMPEG, FFMPEG_DIR, FFPROBE, STATE_DIR, WORKDIR)




DEFAULT_UID = "2137589551"          # 李大霄
LOCK_STALE_SECONDS = 8 * 3600       # 锁文件超过8小时视为陈旧

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


from textutil import (  # noqa: E402
    filter_ocr_text, log, norm_text, read_text, write_text)


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
        "llm_free": {                           # 免费层: OpenCode Zen 免密车道 (无需任何 key)
            "enabled": True,
            "provider": "opencode-zen-free",    # 走 Zen 公共免密网关, 固定 Bearer public + OpenCode 指纹头
            "base_url": "https://opencode.ai/zen/v1",
            "model": "mimo-v2.6-flash-free",
            "temperature": 0.3,
            "max_tokens": 32768,                # 该车道 reasoning_effort 是空操作, "max"=把 max_tokens 顶满
            "timeout": 300,
        },
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
        "llm_local": {                          # 本地权重 (llama.cpp OpenAI 兼容), 校验不过自动回退云端
            "enabled": False,
            "provider": "local",
            "base_url": "http://127.0.0.1:8080/v1",
            "model": "minicpm5-2b",
            "temperature": 0.3,
            "max_tokens": 3000,
            "timeout": 900,
            "enable_thinking": False,           # MiniCPM5 混合思考: 关掉防止全花在 reasoning 上
            "validate": True,                   # 结构校验(6 段 + 结尾声明), 不过就回退
        },
        "vision_free": {                        # 片尾识别第一层: MiMo 免费视觉车道 (无 key)
            "enabled": True,
            "provider": "opencode-zen-free",
            "base_url": "https://opencode.ai/zen/v1",
            "model": "mimo-v2.6-flash-free",
            "max_tokens": 8000,                 # 思考不可关, 需留足思考 token
            "timeout": 240,
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
        "vision_cli": {                         # 片尾识别第三级: deepseek-vision-cli (DeepSeek 网页版「识图模式」)
            "enabled": True,
            "provider": "deepseek-vision-cli",
            "repo": "https://github.com/menghuanshiguang/deepseek-vision-cli.git",
            "path": "_repo/deepseek-vision-cli",
            "token_env": "DSV_TOKEN",
            "model": "deepseek-vision-web",     # 仅报告展示用 (网页版无模型名)
            "timeout": 240,                     # 回答等待上限(秒), 经 DSV_TIMEOUT 传给 dsv.py
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
        # 先深合并嵌套段再整体更新: cfg.update(user) 会把嵌套 dict 整体替换掉,
        # 导致后面的 cfg[k].update(user[k]) 变成 no-op, 用户配置缺省字段的默认值全部丢失
        for k in ("llm_free", "llm", "llm_fallback", "llm_cli", "llm_ollama", "llm_local",
                  "vision_free", "vision", "vision_cli", "vision_fallback"):
            if isinstance(user.get(k), dict):
                cfg[k].update(user.pop(k))
        cfg.update(user)
    # 本地模式开关: 环境变量 LIDAXIAO_LOCAL=1 或 config.json local_ollama=true
    cfg["local_ollama"] = bool(user.get("local_ollama", False)) if os.path.exists(p) else False
    if os.environ.get("LIDAXIAO_LOCAL") == "1":
        cfg["local_ollama"] = True
    return cfg


LLM_PROVIDER_KEY_ENVS = {
    "opencode-go": ("OPENCODE_GO_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "deepseek-chat-cli": ("DSV_TOKEN", "DEEPSEEK_CHAT_CLI_TOKEN"),
    "deepseek-vision-cli": ("DSV_TOKEN",),
}
LLM_KEY_ENVS = ("OPENCODE_GO_API_KEY", "DEEPSEEK_API_KEY", "DSV_TOKEN")
# OpenCode Zen 免密车道: 固定公开 token, 不需要任何环境变量
FREE_PROVIDER = "opencode-zen-free"
# 片尾视觉 CLI 车道: DeepSeek 网页版「识图模式」(走登录 token, 不需要 API key)
VISION_CLI_PROVIDER = "deepseek-vision-cli"


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
                if line.startswith("#") or "=" not in line:
                    continue
                # 精确匹配变量名, 避免 DSV_TOKEN 误匹配 DSV_TOKEN2 等同前缀键
                key, val = line.split("=", 1)
                if key.strip() == name:
                    return val.strip().strip("'\"")
    return ""


def load_llm_key(provider=None):
    """密钥优先级: 环境变量 > .env 文件; provider=None 时按 OPENCODE_GO_API_KEY > DEEPSEEK_API_KEY 顺序"""
    if provider == FREE_PROVIDER:
        return "public"        # 免密车道固定 token, 走 OpenCode 指纹头校验
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
from sources import (  # noqa: E402
    _direct_download, download_video, ensure_complete_download, expected_duration,
    fetch_video_info, find_downloaded_mp4, get_space_videos, run_bilidown)


# ---------------- B站 API (view 信息) ----------------






# =====================================================================
# 1. 更新检测
# =====================================================================




# =====================================================================
# 2. 视频下载
# =====================================================================










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


from ocr import (  # noqa: E402
    extract_frames, ffprobe_duration, ocr_frame, probe_band_heuristic,
    probe_subtitle_band)








# 频道水印: 整帧扫描(片尾补扫/兜底)会把左上角“李大霄 bilibili”读进字幕,
# 这些词在口播正文里不会出现, 直接剥掉; 中文名只在其后紧跟拉丁字母时才算水印,
# 避免误伤正文里提到的“李大霄”。








def ocr_video(cfg, video_path, bvid):
    """返回 (字幕文本, 统计) ; 文本为空表示无字幕

    引擎选择: config.ocr_engine = "xiaomi" 时先起常驻 llama-server 跑 Xiaomi-OCR-0,
    起不来/中途失效自动回退 RapidOCR。
    """
    xm = None
    eng = (os.environ.get("LIDAXIAO_OCR_ENGINE") or cfg.get("ocr_engine") or "rapid").lower()
    if eng == "xiaomi":
        try:
            from xiaomi_ocr import XiaomiOCR
            xm = XiaomiOCR(cfg.get("xiaomi_ocr") or {}, log=log)
            if not xm.start():
                log("⚠ Xiaomi-OCR 启动失败, 本次回退 RapidOCR")
                xm = None
        except Exception as e:
            log(f"⚠ Xiaomi-OCR 不可用({e}), 回退 RapidOCR")
            xm = None
    elif eng == "aoyuon":
        try:
            from aoyuon_ocr import AoyuonOCR
            xm = AoyuonOCR(cfg.get("aoyuon") or {}, log=log)
            if not xm.start():
                log("⚠ Aoyuon-OCR 启动失败, 本次回退 RapidOCR")
                xm = None
            else:
                log("Aoyuon-OCR 已加载 (19.7M int8)")
        except Exception as e:
            log(f"⚠ Aoyuon-OCR 不可用({e}), 回退 RapidOCR")
            xm = None
    try:
        return _ocr_video_impl(cfg, video_path, bvid, xm)
    finally:
        if xm is not None:
            xm.stop()


def _ocr_video_impl(cfg, video_path, bvid, xm=None):
    from difflib import SequenceMatcher
    engine = xm or get_ocr()
    conf = cfg["ocr_confidence"]
    dur = ffprobe_duration(video_path)
    interval = cfg.get("frame_interval") or max(1.0, min(4.0, dur / 160.0))
    frames = extract_frames(video_path, bvid, interval)
    if not frames:
        log("抽帧失败, 无帧可OCR")
        return "", {"frames": 0}
    stats = {"frames": len(frames), "ocr_ok": 0, "ocr_empty": 0,
             "engine": getattr(xm, "name", None) or ("xiaomi" if xm is not None else "rapid")}
    log(f"OCR 引擎: {stats['engine']}")

    dedup_on = bool(cfg.get("ocr_dedup", False))
    env_dedup = os.environ.get("LIDAXIAO_OCR_DEDUP")
    if env_dedup not in (None, ""):
        dedup_on = env_dedup.strip().lower() in ("1", "true", "yes", "on")
    if dedup_on:
        log("帧去重: 开启 (字幕带差分, 每段只 OCR 中间帧)")

    def scan(frames_idx, band):
        """对给定帧序列做 OCR, 返回 [(ts, line)]"""
        nonlocal engine
        out = []
        last_norm = ""
        idx_list = list(frames_idx)
        seq = [(i, i) for i in idx_list]
        if dedup_on and len(idx_list) > 1:
            try:
                import frame_dedup as _fd
                sigs = [_fd.band_sig(frames[i], band) for i in idx_list]
                seq = _fd.pick_indices(sigs)
                stats["ocr_calls"] = stats.get("ocr_calls", 0) + len(seq)
                log(f"帧去重: {len(idx_list)} 帧 -> {len(seq)} 次 OCR "
                    f"(省 {100 - len(seq) * 100 // max(1, len(idx_list))}%)")
            except Exception as e:
                log(f"⚠ 帧去重失败({e}), 退回逐帧")
                seq = [(i, i) for i in idx_list]
        for i, ts_i in seq:
            if xm is not None and xm.dead:
                log("⚠ Xiaomi-OCR 中途失效, 切换 RapidOCR")
                engine = get_ocr()
                band = probe_subtitle_band(engine, frames, conf)
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
            out.append((int(ts_i * interval), line))
        return out

    # 1) 探测字幕带
    # Xiaomi-OCR: 用配置的固定字幕带(默认下半屏), 既提速又天然避开顶部水印;
    # 未配置时才读整帧。RapidOCR 仍走自适应探测。
    # aoyuon 用整帧: 它的 find_line_boxes 与训练裁图同源, 自己做行检测
    if getattr(xm, "name", "") == "aoyuon":
        band = None
        log("Aoyuon-OCR: 整帧输入(由其自带检测器分行)")
    xcfg = cfg.get("xiaomi_ocr") or {}
    xband = xcfg.get("band") if xm is not None and getattr(xm, "name", "") != "aoyuon" else None
    if xm is not None:
        if xband in (None, "auto", "AUTO") or (
                isinstance(xband, (list, tuple)) and xband and str(xband[0]).lower() == "auto"):
            # 每条视频字幕位置可能不同 -> 让模型先看几帧再定带子
            fb = tuple(xcfg.get("band_fallback") or (0.55, 1.0))
            # Xiaomi 不返回真实坐标 -> 用画面行投影定位, 失败回退默认带
            band = probe_band_heuristic(frames) or fb
            stats["band_auto"] = True
            stats["band"] = band
        else:
            band = tuple(xband)
    else:
        band = probe_subtitle_band(engine, frames, conf)

    # 2) 主扫描
    out_lines = scan(range(len(frames)), band)
    log(f"主扫描完成: 有效 {len(out_lines)} 条/{len(frames)} 帧")

    # 2.5) 片尾补扫: 声明卡/荐书卡比口播字幕高得多, 紧凑带会切掉上沿 -> 用整帧补最后 10%
    if band and stats.get("band_auto"):
        tail_n = max(8, len(frames) // 10)
        extra = scan(range(max(0, len(frames) - tail_n), len(frames)), None)
        if extra:
            out_lines = merge_ts_lines(extra, out_lines)   # 同 ts 时整帧结果优先
            log(f"片尾补扫(整帧): {len(extra)} 条")

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
from prompts import (ANALYZE_SYSTEM, ENDING_PROMPT, ENDING_PROMPT_VER, REPORT_SECTIONS, REPORT_TAIL)  # noqa: E402


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


# =====================================================================
# OpenCode Zen 免密车道 (Bearer public + OpenCode 客户端指纹 + 流式)
# 协议实测自 zouyuxuan122/dsh-our-free-model:
#   - 非流式必挂(500/403), 只有 stream:true 稳定 200
#   - body 必须带 bash/glob/grep/read 四个最小 tools + tool_choice:none, 否则 403
#   - reasoning_effort 发了是空操作, 思考与正文抢 max_tokens -> 预算靠 max_tokens 顶
#   - 免费额度按 session 计: ses_/msg_ id 用 sha256 稳定派生, 不能每请求新铸
# =====================================================================
FREE_B62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
FREE_DECOY_TOOLS = [
    {"type": "function", "function": {"name": n, "description": "d",
     "parameters": {"type": "object", "properties": {}}}}
    for n in ("bash", "glob", "grep", "read")
]


def _free_derive_id(prefix, seed):
    """sha256 派生稳定 id: 前6字节hex + 后14字节base62 (与插件同构)"""
    h = hashlib.sha256(seed.encode("utf-8")).digest()
    return prefix + h[:6].hex() + "".join(FREE_B62[b % 62] for b in h[6:20])


def _free_lane_post(pcfg, payload, timeout):
    """免密车道流式 POST, 返回拼接好的正文 (跳过 reasoning 增量)"""
    import requests
    seed = "lidaxiao-monitor\0" + (pcfg.get("model") or "free")
    headers = {
        "Authorization": "Bearer public",
        "User-Agent": "opencode/1.18.31",
        "x-opencode-client": "desktop",
        "x-opencode-session": _free_derive_id("ses_", seed),
        "x-opencode-request": _free_derive_id(
            "msg_", seed + "\0" + str(time.time()) + str(os.getpid())),
        "x-opencode-project": "global",
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
    }
    body = dict(payload, stream=True, tools=FREE_DECOY_TOOLS, tool_choice="none")
    url = pcfg["base_url"].rstrip("/") + "/chat/completions"
    r = requests.post(url, json=body, headers=headers, timeout=timeout, stream=True)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
    r.encoding = "utf-8"          # iter_lines 默认 latin-1, 中文正文会乱码
    parts, finish = [], ""
    for line in r.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        s = line[5:].strip()
        if s == "[DONE]":
            break
        try:
            j = json.loads(s)
        except ValueError:
            continue
        for ch in j.get("choices") or []:
            delta = ch.get("delta") or {}
            if delta.get("content"):
                parts.append(delta["content"])
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
    text = "".join(parts).strip()
    if not text:
        raise RuntimeError(f"免密车道返回空正文 (finish={finish})")
    if finish == "length":
        raise RuntimeError(f"免密车道正文被 max_tokens={payload.get('max_tokens')} 截断")
    return text


def vision_providers(cfg):
    """视觉 provider 优先级(固定): MiMo 免密 -> opencode-go -> deepseek-vision-cli -> DeepSeek 官方 API

    deepseek-vision-cli (dsv.py) 走 DeepSeek 网页版「识图模式」, 用登录 token,
    不消耗 API 额度; 多帧一次会话发送, 用后即删。
    """
    out = []
    for k in ("vision_free", "vision", "vision_cli", "vision_fallback"):
        p = cfg.get(k)
        if not p or not p.get("enabled", True):
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

    if pcfg.get("provider") == FREE_PROVIDER:
        # 免密车道: 非流式必挂, 走流式分支 (思考不可关, max_tokens 留足)
        return _free_lane_post(pcfg, {
            "model": pcfg["model"],
            "messages": [{"role": "user", "content": content}],
            "max_tokens": pcfg.get("max_tokens", 8000),
        }, pcfg.get("timeout", 240))

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
            if provider == VISION_CLI_PROVIDER:
                # 网页版识图 CLI: 帧文件直接作为参数传给 dsv.py (多图一次发送)
                text = _call_dsv(pcfg, key, ENDING_PROMPT, picks)
            else:
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


def check_vision(cfg):
    """自检视觉通道: 打印每个 provider 的可用性 (手动排障用, 定时任务不再调用)"""
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
            if provider == VISION_CLI_PROVIDER:
                text = _call_dsv(pcfg, key, "这张图里有什么文字? 直接回答, 不要解释。",
                                 frames[:1])
            else:
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
    if pcfg.get("mode") == "json":
        import local_render
        return local_render.run(pcfg, user)
    if pcfg.get("provider") == "ollama":
        return _call_ollama_native(pcfg, system, user)
    import requests
    _warn_model(pcfg)
    url = pcfg["base_url"].rstrip("/") + "/chat/completions"
    if pcfg.get("provider") == FREE_PROVIDER:
        # 免密车道: 非流式必挂, 走流式 + 指纹头 + decoy tools
        return _free_lane_post(pcfg, {
            "model": pcfg["model"],
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": pcfg.get("max_tokens", 4000),
        }, pcfg.get("timeout", 180))
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
    if pcfg.get("provider") in ("local", "llama") and pcfg.get("enable_thinking") is False:
        # MiniCPM5 混合思考: 关掉, 否则容易把 token 全花在 reasoning 上、正文为空
        payload["chat_template_kwargs"] = {"enable_thinking": False}
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
    msg = j["choices"][0]["message"]
    content = (msg.get("content") or "").strip()
    if not content:
        reason = msg.get("reasoning_content") or ""
        raise RuntimeError(f"返回空 content (reasoning {len(reason)} chars)")
    return content


def _find_cli_path(pcfg, fname, dirname):
    """定位 CLI 仓库的入口脚本 (dsc.py / dsv.py)"""
    rel = pcfg.get("path", "_repo/" + dirname)
    cands = []
    if os.path.isabs(rel):
        cands.append(rel)
    else:
        cands.append(os.path.join(WORKDIR, rel))
    for extra in ("_repo/" + dirname, "tools/" + dirname, dirname):
        cands.append(os.path.join(WORKDIR, extra))
    for d in cands:
        f = os.path.join(d, fname)
        if os.path.isfile(f):
            return f
    return None


def _find_dsc_path(pcfg):
    """定位 deepseek-chat-cli 的 dsc.py"""
    return _find_cli_path(pcfg, "dsc.py", "deepseek-chat-cli")


def _find_dsv_path(pcfg):
    """定位 deepseek-vision-cli 的 dsv.py"""
    return _find_cli_path(pcfg, "dsv.py", "deepseek-vision-cli")


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


def _clone_dsv(pcfg):
    """克隆 deepseek-vision-cli (公开仓库, 零额外依赖: iSH 用系统 minis-browser-use / Windows 用 playwright+Edge)"""
    repo = pcfg.get("repo", "https://github.com/menghuanshiguang/deepseek-vision-cli.git")
    rel = pcfg.get("path", "_repo/deepseek-vision-cli")
    dest = rel if os.path.isabs(rel) else os.path.join(WORKDIR, rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        subprocess.run(["git", "clone", "--depth", "1", repo, dest],
                       check=True, capture_output=True, timeout=180)
        return True
    except Exception as e:
        log(f"⚠️ git clone deepseek-vision-cli 失败: {e}")
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


def _call_dsv(pcfg, token, prompt, images):
    """调用 deepseek-vision-cli (dsv.py): DeepSeek 网页版「识图模式」, 多帧一次会话发送

    images: 本地图片路径列表 (帧文件), 与 prompt 一起作为 dsv.py 参数。
    """
    dsv_py = _find_dsv_path(pcfg)
    if not dsv_py:
        log("⏬ deepseek-vision-cli 未找到, 尝试克隆...")
        if not _clone_dsv(pcfg):
            raise RuntimeError("deepseek-vision-cli 克隆失败")
        dsv_py = _find_dsv_path(pcfg)
        if not dsv_py:
            raise RuntimeError("克隆后仍未找到 dsv.py")

    tok = (token or "").strip().lstrip("\ufeff")
    if not tok:
        # 兜底: 直接读仓库自带的 .dsv_token
        repo_tok = os.path.join(os.path.dirname(dsv_py), ".dsv_token")
        if os.path.exists(repo_tok):
            with open(repo_tok, "r", encoding="utf-8-sig") as f:
                tok = f.read().strip()
    if not tok:
        raise RuntimeError("deepseek-vision-cli: 未找到 DSV_TOKEN")

    # dsv.py 固定读 ~/.dsv_token, 把系统变量写进去 (与 deepseek-chat-cli 共用同一登录态)
    with open(os.path.expanduser("~/.dsv_token"), "w", encoding="utf-8") as f:
        f.write(tok)

    # 每次调用使用全新浏览器 profile, 避免连续多次调用后检测退化导致超时 (与 dsc 同策略)
    import uuid
    profile_dir = os.path.join(WORKDIR, "data", "dsv_profiles", uuid.uuid4().hex)
    os.makedirs(profile_dir, exist_ok=True)

    cmd = [sys.executable, os.path.abspath(dsv_py)]
    cmd += [os.path.abspath(p) for p in images]
    cmd.append(prompt)
    log(f"调用 deepseek-vision-cli ({os.path.basename(dsv_py)}, {len(images)}帧)...")
    # dsv.py 支持 DSV_TIMEOUT 控制回答等待时长; DSV_EDGE_PROFILE 指定本次全新 profile (Windows)
    env = dict(os.environ)
    env["DSV_TIMEOUT"] = str(pcfg.get("timeout", 240))
    env["DSV_EDGE_PROFILE"] = profile_dir
    try:
        res = subprocess.run(cmd, cwd=os.path.dirname(dsv_py), capture_output=True,
                             text=True, encoding="utf-8", errors="replace",
                             timeout=pcfg.get("timeout", 240) + 90, env=env)
    except subprocess.TimeoutExpired:
        raise RuntimeError("deepseek-vision-cli 调用超时")
    finally:
        # 用完即删, 避免 data/dsv_profiles 无限膨胀
        shutil.rmtree(profile_dir, ignore_errors=True)
    if res.returncode != 0:
        tail = (res.stderr or res.stdout or "").strip()[-500:]
        if res.returncode == 4:
            raise RuntimeError(f"deepseek-vision-cli: DSV_TOKEN 无效/缺失, 需重新登录: {tail}")
        if res.returncode == 5:
            raise RuntimeError(f"deepseek-vision-cli: 另一个 dsv 正在运行(锁占用): {tail}")
        raise RuntimeError(f"deepseek-vision-cli 失败 exit={res.returncode}: {tail}")
    out = (res.stdout or "").strip()
    if not out:
        raise RuntimeError("deepseek-vision-cli 返回空内容")
    # dsv.py 超时/被拒时会以退出码 0 输出提示文本, 必须识别为失败并继续兜底
    if "超时未获取回答" in out or "未获取回答" in out:
        raise RuntimeError("deepseek-vision-cli 超时未获取回答")
    if "DeepSeek拒绝处理" in out:
        raise RuntimeError(f"deepseek-vision-cli 内容被拒: {out[:200]}")
    return out




def validate_report(text, reference=""):
    """本地小模型结构校验: 7 段 + 结尾声明都要在, 否则视为不合格回退云端

    reference 传入原始 prompt(含字幕), 用来抽查数字是否来自本期内容,
    防止小模型把历史/别期的数据张冠李戴。
    """
    t = (text or "").strip()
    miss = [s for s in REPORT_SECTIONS if s not in t]
    if len(t) < 400:
        miss.append("内容过短")
    if REPORT_TAIL not in t:
        miss.append("缺结尾声明")
    # 复读提示词 / 模板占位没替换
    for bad in ("每条必须包含原文数据", "格式: N.", "【输出模板】", "3-8条"):
        if bad in t:
            miss.append("复读提示词")
            break
    if re.search(r"(?m)^N\.\s", t):
        miss.append("模板占位N未替换")
    # 数字溯源: 输出里的多位数应当能在本期 prompt(字幕)里找到
    if reference:
        nums = [n for n in re.findall(r"\d+(?:\.\d+)?(?:%|万亿|亿|万)?", t)
                if len(n.rstrip("%万亿亿点")) >= 2]
        if nums:
            hit = sum(1 for n in set(nums) if n in reference)
            if hit / len(set(nums)) < 0.5:
                miss.append(f"数据不在本期字幕({hit}/{len(set(nums))})")
    return (not miss), miss


def _local_llm_alive(pcfg, timeout=3):
    """本地 llama.cpp 探活: 没起就不进 provider 链, 免得每次白等 900s 超时"""
    import requests
    base = (pcfg.get("base_url") or "").rstrip("/")
    if not base:
        return False
    root = base[:-3] if base.endswith("/v1") else base
    # llama.cpp 有 /health; Ollama 只有 /v1/models 或 /api/tags
    for url in (root + "/health", base.rstrip("/") + "/models", root + "/api/tags"):
        try:
            if requests.get(url, timeout=timeout).status_code < 400:
                return True
        except Exception:
            continue
    return False


def call_llm(cfg, api_key, system, user, dsc_prompt=None):
    """按顺序尝试 LLM provider; 本地模式(cfg.local_ollama)只走 Ollama qwen2.5:7b

    文本优先级(固定): opencode-go -> deepseek-chat-cli -> DeepSeek 官方 API
    (即 cfg.llm -> cfg.llm_cli -> cfg.llm_fallback, 顺序由 providers 列表决定)
    """
    if cfg.get("local_ollama"):
        providers = [cfg.get("llm_ollama")]
    else:
        providers = []
        local = cfg.get("llm_local")
        if (local and local.get("enabled") and _local_llm_alive(local)
                and local.get("position", "first") == "first"):
            providers.append(local)            # 本地优先: 校验不过自动回落云端
        free = cfg.get("llm_free")
        if free and free.get("enabled", True):
            providers.append(free)             # 免费层最前, 挂了自动落到下面三级
        providers.append(cfg.get("llm"))
        cli = cfg.get("llm_cli")
        if cli and cli.get("provider") != cfg.get("llm", {}).get("provider"):
            providers.append(cli)
        fallback = cfg.get("llm_fallback")
        if fallback and fallback.get("provider") != cfg.get("llm", {}).get("provider"):
            providers.append(fallback)
        if (local and local.get("enabled") and local.get("position") == "last"
                and _local_llm_alive(local)):
            providers.append(local)            # 可选: position=last 时退居兜底
    NO_KEY_PROVIDERS = {"ollama", "local", "llama"}
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
            if provider in ("local", "llama") and pcfg.get("validate", True):
                ref = user
                ok, miss = validate_report(out, ref)
                if not ok:
                    tag = "本地模型" if provider in ("local", "llama") else provider
                    log(f"⚠️ {tag} 输出不完整({', '.join(miss)}), 换下一个通道")
                    last_err = f"{provider}: 校验未过 {miss}"
                    continue
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
【关键数据清单】列出视频中出现的具体数据: 美债收益率/巴菲特指标/见顶时间表/指数点位/成交量等, 格式: - 项目: 数值（必须带单位; 纯日期/"周六周日"/"康波周期"不算数据; 没有写"本期无量化数据"）
【暗示提取】逐条列出视频中带暗示性质的话术并判断方向, 每条标注: 🔴警示 / 🟢看多 / ⚪中性 / ⚠️风险; reason 必须引用该条原文的关键要素, 禁止多条复用同一句。注意"不是推荐"是李大霄的核心反话, 要逐条看语境: 光杆点名型(主动拎出某标的、无跌幅铺垫, 如历史兑现的韩国/恒科/银行/券商)多为隐性推荐, 按标的属性给方向; 报跌幅型("XX跌了X%, 不是推荐", 尤其海外行情/板块复盘语境)就是字面意思——风险提示该板块, 标🔴警示, 绝不写成"关注价值/超跌机会"; 片尾免责声明里的"并非推荐"标⚪中性。禁止一律套用"实则暗示关注", 禁止虚构"暗示意味最强"之类强度评语。若上面给了片尾画面, 末尾按顺序补两条: 一条以"片尾:"开头(文字/卡片信号), 一条以"片尾图形:"开头(画面图形隐喻, 按"形态 → 含义 → 方向"写, 方向用 🔴高位风险/看空、🟢低位机会/看多)
【市场定性】反弹/反转/见顶/调整/防御/无明确判断 之一 + 依据。依据必须引用本期字幕里的点位/涨跌幅/量能/估值等具体证据; 纯理念/生活/投教类且无数值证据时必须写"无明确判断, 本期无数值依据"。注意他的核心框架: "没量=不是反转"。
【操作含义】三行, 必须可执行:
  动作: 加仓/减仓/持有/观望/不适用 之一
  仓位: 具体比例(如"3成仓") 或 "不适用"
  触发: 可执行条件(如"放量站上3200点") 或 "不适用"
  禁止"适度/合理/注意/长期持有"这类无法执行的词。其中方向、仓位、风险里的关键词（如看多/看空/观望、加仓/减仓、进攻/防御等）请用 ~关键词~ 这种波浪号标记标出（例如 ~防御~、~看空~、~减仓~），我会在后处理时转成加粗。
【反照抄】历史报告仅用于【观点连续性】, 严禁把历史报告的结论/措辞/案例搬进本期摘要、定性或操作。
【观点连续性】与历史报告对比: 标注 延续/升级/新增/反转。如态度升级(如"警惕→高度警惕")必须重点标出。无历史报告则写"首次记录,无历史对比"。

最后必须另起一行输出：以上为李大霄个人观点提炼,不构成投资建议"""



def _tnorm(t):
    return re.sub(r"[\s,，。.!！?？、]", "", t or "")


def _tdedup(items, key):
    out = []
    for it in items:
        k = key(it)
        if not k:
            continue
        for i, prev in enumerate(out):
            if k == key(prev):
                if len(it) > len(prev):
                    out[i] = it
                break
        else:
            out.append(it)
    return out


def tighten_analysis(text, subtitle="", history=""):
    """审计收敛: 去重 / 数据规范 / 操作三件套 / 定性证据与照抄 / 去视觉泄漏"""
    t = (text or "").strip()
    t = re.sub(r"\s*[(（]视觉[:：][^)）]*[)）]", "", t)

    def blk(name):
        return re.search(rf"【{name}】(.*?)(?=【|\Z)", t, re.S)

    m = blk("核心观点")
    if m:
        rows = re.findall(r"(?m)^\s*\d+\.\s*(.+)$", m.group(1))
        if rows:
            kept = _tdedup(rows, lambda x: _tnorm(re.split(r"[(（]原文数据", x)[0])[:40])
            bod = "\n" + "\n".join(f"{i}. {x}" for i, x in enumerate(kept, 1)) + "\n"
            t = t[:m.start(1)] + bod + t[m.end(1):]

    m = blk("暗示提取")
    if m:
        rows = re.findall(r"(?m)^\s*\d+\.\s*(.+)$", m.group(1))
        if rows:
            seen, kept = set(), []
            for r in rows:
                q = re.search(r"“(.+?)”", r)
                k = _tnorm(q.group(1)) if q else _tnorm(r)[:30]
                if k in seen:
                    continue
                seen.add(k)
                kept.append(re.sub(r"[:：]\s*(李大霄的提醒|明确提醒某个风险点.*)$", "", r))
            bod = "\n" + "\n".join(f"{i}. {x}" for i, x in enumerate(kept, 1)) + "\n"
            t = t[:m.start(1)] + bod + t[m.end(1):]

    m = blk("关键数据清单")
    if m:
        rows = [r.strip().rstrip(",，") for r in re.findall(r"(?m)^\s*-\s*(.+)$", m.group(1))]
        good = []
        for r in rows:
            if not re.search(r"\d", r) or re.fullmatch(r"\d{4}年.*", r):
                continue
            if re.match(r"^(周六|周日|康波)", r) or (":" not in r and "：" not in r):
                continue
            good.append(r)
        good = _tdedup(good, lambda x: _tnorm(x))
        bod = "\n" + ("\n".join(f"- {x}" for x in good) if good else "- 本期无量化数据") + "\n"
        t = t[:m.start(1)] + bod + t[m.end(1):]

    m = blk("市场定性")
    if m:
        val = m.group(1).strip()
        kinds = ("反弹", "反转", "见顶", "调整", "防御", "无明确判断")
        kind = next((k for k in kinds if k in val[:12]), "")
        has_ev = bool(re.search(r"\d+(\.\d+)?(%|点|万亿|亿|万|倍)", subtitle or ""))
        reason = re.sub(r"^.*?[:：]", "", val).strip()
        copied = bool(history) and len(reason) >= 8 and re.sub(r"[\s，。,.、]", "", reason) in re.sub(r"[\s，。,.、]", "", history)
        if not has_ev:
            val = "无明确判断：本期无点位/涨跌幅/量能等数值依据"
        elif copied:
            val = "无明确判断：定性理由与历史报告重复(疑似照抄)，本期无独立依据"
        elif not kind:
            val = "无明确判断：" + (reason or "依据不足")
        t = t[:m.start(1)] + "\n" + val + "\n" + t[m.end(1):]

    m = blk("操作含义")
    if m:
        body = m.group(1)
        d = re.search(r"方向[*\s]*[:：]\s*(.+)", body)
        pz = re.search(r"仓位[*\s]*[:：]\s*(.+)", body)
        rk = re.search(r"风险[*\s]*[:：]\s*(.+)", body)
        act = (d.group(1).strip() if d else "")
        if "观望" in act: act = "观望"
        elif "空" in act: act = "减仓"
        elif "多" in act: act = "持有"
        elif act in ("加仓","减仓","持有","不适用"): pass
        else: act = "不适用"
        pos = (pz.group(1).strip() if pz else "不适用")
        if not re.search(r"\d", pos) or re.search(r"(长期持有|适度|合理|注意)", pos):
            pos = "不适用"
        trig = (rk.group(1).strip() if rk else "不适用")
        if re.search(r"(适度|合理|注意)", trig):
            trig = "不适用"
        t = t[:m.start(1)] + f"\n- **动作**: {act}\n- **仓位**: {pos}\n- **触发**: {trig}\n" + t[m.end(1):]

    if _audit:
        t = _audit.fix_point_context(t)
        t = _audit.fix_continuity(t, history)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


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
    out = call_llm(cfg, api_key, sys_prompt, user, dsc_prompt=dsc_prompt)
    return tighten_analysis(out, subtitle_text, history)


# =====================================================================
# 5. 每日报告
# =====================================================================
from report import (  # noqa: E402
    build_section, ensure_disclaimer, postprocess_analysis, upsert_report)












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
    free_ok = (cfg.get("llm_free") or {}).get("enabled", True)
    if not cfg.get("local_ollama") and not api_key and not free_ok:
        log("❌ 未找到 LLM 密钥 (OPENCODE_GO_API_KEY / DEEPSEEK_API_KEY, 检查 .env 或环境变量)")
        return 1
    if not api_key and free_ok:
        log("ℹ️ 无本地密钥, 仅免费层可用 (opencode-zen-free)")

    lock = RunLock(os.path.join(WORKDIR, "data", "monitor.lock"))
    if not lock.acquire():
        return 0
    try:
        # 1. 检测更新
        videos = get_space_videos(cfg, cfg["limit"])
        if not videos:
            log("❌ 无法获取UP主视频列表(风控或网络问题)")
            return 1
        # 只处理 baseline(前天)及之后发布的视频, 更早的忽略 (--force 豁免: 始终取最新)
        if not args.force:
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
