"""片尾识别: 片尾抽帧 / 缓存 / 视觉通道调用 — 从 monitor.py 抽出

李大霄视频片尾固定是"卡片区": 口播结束语 + 荐书卡 + 合规声明卡(+偶尔数据面板),
这类整屏密集文字会被 OCR 的字数/数字过滤规则丢弃, 因此单独用视觉模型读片尾。
"""
import datetime
import glob
import json
import os
import subprocess
import time

from lidaxiao.common import CN_TZ, FFMPEG, WORKDIR
from lidaxiao.config import load_llm_key
from lidaxiao.ocr.frames import ffprobe_duration
from lidaxiao.ocr.pipeline import dedupe_frames
from lidaxiao.prompts import ENDING_PROMPT, ENDING_PROMPT_VER
from lidaxiao.textutil import log, read_text, write_text

# 与 lidaxiao.common.STATE_DIR 同一个算法、同一个值 (本轮口径: 不把它挪进 common.py)
STATE_DIR = os.path.join(WORKDIR, "state")

# 过渡依赖 (P3 阶段裁定): 视觉通道 vision_providers / _call_vision_once / _call_dsv /
# VISION_CLI_PROVIDER 仍在 analyze/llm.py, 交接文档 §4 把这些写成"后续合并"的工作, 本轮不做。
# 所以 vision -> analyze 是一条临时反向边: 硬约束是 analyze 不得反向 import vision,
# vision 不得 import sources/report/monitor。视觉通道搬进本包后删除这一段并单开 commit。
from lidaxiao.analyze.llm import (  # noqa: E402
    VISION_CLI_PROVIDER, _call_dsv, _call_vision_once, vision_providers)


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
