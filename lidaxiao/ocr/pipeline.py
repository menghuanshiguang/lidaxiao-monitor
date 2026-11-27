"""OCR 流水线: 引擎选择与回退 / 逐帧识别 / 帧去重 — 从 monitor.py 抽出

依赖同层的 ocr.engine 与 ocr.frames, 以及 textutil/common; 不 import monitor。
"""
import os

from lidaxiao.common import WORKDIR
from lidaxiao.ocr.engine import get_ocr
from lidaxiao.ocr.frames import (
    extract_frames, ffprobe_duration, ocr_frame, probe_band_heuristic,
    probe_subtitle_band)
from lidaxiao.textutil import filter_ocr_text, log, norm_text, write_text


def ocr_video(cfg, video_path, bvid):
    """返回 (字幕文本, 统计) ; 文本为空表示无字幕

    引擎选择: config.ocr_engine = "xiaomi" 时先起常驻 llama-server 跑 Xiaomi-OCR-0,
    起不来/中途失效自动回退 RapidOCR。
    """
    xm = None
    eng = (os.environ.get("LIDAXIAO_OCR_ENGINE") or cfg.get("ocr_engine") or "rapid").lower()
    if eng == "xiaomi":
        try:
            from lidaxiao.ocr.engines.xiaomi import XiaomiOCR
            xm = XiaomiOCR(cfg.get("xiaomi_ocr") or {}, log=log)
            if not xm.start():
                log("⚠ Xiaomi-OCR 启动失败, 本次回退 RapidOCR")
                xm = None
        except Exception as e:
            log(f"⚠ Xiaomi-OCR 不可用({e}), 回退 RapidOCR")
            xm = None
    elif eng == "aoyuon":
        try:
            from lidaxiao.ocr.engines.aoyuon import AoyuonOCR
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
                from lidaxiao.ocr import dedup as _fd
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
