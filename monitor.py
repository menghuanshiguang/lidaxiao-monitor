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
from lidaxiao.common import (  # noqa: E402
    CN_TZ, DISCLAIMER, STATE_DIR, WORKDIR)

try:
    import audit_p2 as _audit
except Exception:  # pragma: no cover - 模块缺失时不影响主流程
    _audit = None
# bash 路径可用环境变量 BILIDOWN_BASH 覆盖 (如 WSL/GitBash 安装在不同位置)
BASH = os.environ.get("BILIDOWN_BASH", r"D:\Program Files\Git\bin\bash.exe")
BILIDOWN_SCRIPT = os.path.join(WORKDIR, "_repo", "bin", "bilidown")
from lidaxiao.common import (  # noqa: E402
    CN_TZ, FFMPEG, FFMPEG_DIR, FFPROBE, STATE_DIR, WORKDIR)




LOCK_STALE_SECONDS = 8 * 3600       # 锁文件超过8小时视为陈旧

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


from lidaxiao.textutil import (  # noqa: E402
    filter_ocr_text, log, norm_text, read_text, write_text)


from lidaxiao.config import (  # noqa: E402
    load_config, load_llm_key)


# OpenCode Zen 免密车道: 固定公开 token, 不需要任何环境变量
# 片尾视觉 CLI 车道: DeepSeek 网页版「识图模式」(走登录 token, 不需要 API key)








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
from lidaxiao.sources.bilibili import (  # noqa: E402
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


from lidaxiao.ocr.frames import (  # noqa: E402
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
from lidaxiao.prompts import (ANALYZE_SYSTEM, ENDING_PROMPT, ENDING_PROMPT_VER, REPORT_SECTIONS, REPORT_TAIL)  # noqa: E402


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




# =====================================================================
# OpenCode Zen 免密车道 (Bearer public + OpenCode 客户端指纹 + 流式)
# 协议实测自 zouyuxuan122/dsh-our-free-model:
#   - 非流式必挂(500/403), 只有 stream:true 稳定 200
#   - body 必须带 bash/glob/grep/read 四个最小 tools + tool_choice:none, 否则 403
#   - reasoning_effort 发了是空操作, 思考与正文抢 max_tokens -> 预算靠 max_tokens 顶
#   - 免费额度按 session 计: ses_/msg_ id 用 sha256 稳定派生, 不能每请求新铸
# =====================================================================










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


from lidaxiao.analyze.llm import (  # noqa: E402
    VISION_CLI_PROVIDER, _apply_thinking, _call_dsv, _call_vision_once,
    _warn_model, analyze_subtitles, build_dsc_prompt, call_llm, check_vision,
    load_history, tighten_analysis,
    validate_report, vision_providers)


# =====================================================================
# 4. AI 分析
# =====================================================================


# 官方已下线的模型名 -> 现行模型名 (旧名仍可调用, 但请求由 V4.1 Flash 承接)
# 思考强度取值; 官方另接受 minimal/medium/xhigh/ultra (映射到 low/high/max)

















































# =====================================================================
# 5. 每日报告
# =====================================================================
from lidaxiao.report.render import (  # noqa: E402
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
