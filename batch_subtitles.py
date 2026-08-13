#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量字幕提取: 全量处理李大霄视频 (下载480p -> 抽帧 -> OCR底部带 -> 存字幕 -> 删临时文件)
设计要点:
  - 内存: 每 worker 独立进程(~600MB), 默认2个worker, 处理完即释放
  - 磁盘: 每视频的 mp4+帧处理完立即删除, 只保留字幕文本 (~4KB/条)
  - 风控: 下载间隔随机3-8秒, 下载失败重试2次(间隔15s), 连续失败3次暂停120s
  - 断点续传: 字幕文件已存在即跳过, 可随时中断重跑
  - 无字幕视频: 写空文件标记, 重跑不重复处理
用法:
  python batch_subtitles.py                 # 全量, 2 workers
  python batch_subtitles.py --workers 3     # 3 workers (内存够时)
  python batch_subtitles.py --limit 10      # 只处理前10个(测试)
"""
import argparse
import datetime
import glob
import json
import os
import random
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

WORKDIR = os.path.dirname(os.path.abspath(__file__))
if WORKDIR not in sys.path:
    sys.path.insert(0, WORKDIR)
import monitor as m

BOTTOM_BAND = (0.72, 0.99)
MIN_CHARS = 30
SUBTITLE_DIR = os.path.join(WORKDIR, "data", "subtitles")
FRAMES_DIR = os.path.join(WORKDIR, "data", "frames")
DOWNLOAD_DIR = os.path.join(WORKDIR, "downloads")
LOG_FILE = os.path.join(WORKDIR, "data", "batch.log")
FAIL_FILE = os.path.join(WORKDIR, "data", "batch_failed.txt")
LIST_FILE = os.path.join(WORKDIR, "data", "all_videos.json")
UID = "2137589551"

_engine = None


def get_engine():
    global _engine
    if _engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _engine = RapidOCR(intra_op_num_threads=8,
                           det_use_dml=True, cls_use_dml=True, rec_use_dml=True)
    return _engine


def log_line(msg):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")


def fetch_all_videos():
    """拉取全部视频列表 (arc/search 翻页), 返回 [{'bvid','title','created','length'}] 旧的在前"""
    if os.path.exists(LIST_FILE):
        with open(LIST_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if data:
            return data
    import hashlib
    import urllib.parse
    from monitor import bili_api_get
    MIXIN = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5,
             49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55,
             40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57,
             62, 11, 36, 20, 34, 44, 52]
    j = bili_api_get("https://api.bilibili.com/x/web-interface/nav")
    img = j["data"]["wbi_img"]["img_url"].split("/")[-1].split(".")[0]
    sub = j["data"]["wbi_img"]["sub_url"].split("/")[-1].split(".")[0]
    mixin_key = "".join((img + sub)[i] for i in MIXIN)[:32]
    out = []
    page = 1
    while page <= 60:
        params = {"mid": UID, "ps": "50", "pn": str(page), "order": "pubdate",
                  "wts": int(time.time())}
        items = sorted(params.items())
        q = urllib.parse.urlencode(items)
        params["w_rid"] = hashlib.md5((q + mixin_key).encode()).hexdigest()
        url = "https://api.bilibili.com/x/space/wbi/arc/search?" + urllib.parse.urlencode(params)
        d = bili_api_get(url, referer=f"https://space.bilibili.com/{UID}")
        if not d or d.get("code") != 0:
            print(f"列表拉取失败 page={page} code={d and d.get('code')}, 稍后重试", flush=True)
            time.sleep(20)
            continue
        vlist = d["data"]["list"]["vlist"]
        total = d["data"]["page"]["count"]
        out.extend({"bvid": v["bvid"], "title": v["title"],
                    "created": v["created"], "length": v["length"]} for v in vlist)
        print(f"列表: {len(out)}/{total}", flush=True)
        if len(out) >= total or not vlist:
            break
        page += 1
        time.sleep(1.5)
    out.sort(key=lambda v: v["created"])   # 旧的在前
    with open(LIST_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    return out


def ocr_video_fast(video_path, bvid, conf):
    """固定底部带 OCR (批量优化版, 无探测); 返回字幕文本或空串"""
    from difflib import SequenceMatcher
    engine = get_engine()
    dur = m.ffprobe_duration(video_path)
    interval = max(1.5, min(3.0, dur / 120.0))
    frames = m.extract_frames(video_path, bvid, interval)
    if not frames:
        return ""

    def scan(idx_list, band):
        out = []
        last_norm = ""
        for i in idx_list:
            boxes = m.ocr_frame(engine, frames[i], band)
            if not boxes:
                continue
            parts = []
            for _, _, text, score in boxes:
                t = m.filter_ocr_text(text, conf)
                if t and score >= conf:
                    parts.append(t)
            if not parts:
                continue
            line = " ".join(parts)
            n = m.norm_text(line)
            if n == last_norm or (last_norm and
                                  SequenceMatcher(None, n, last_norm).ratio() > 0.85):
                continue
            last_norm = n
            out.append((int(i * interval), line))
        return out

    lines = scan(range(len(frames)), BOTTOM_BAND)
    total = sum(len(l) for _, l in lines)
    if total < MIN_CHARS:
        extra = scan(range(0, len(frames), 6), None)
        lines = m.merge_ts_lines(lines, extra)
        total = sum(len(l) for _, l in lines)
    if total < MIN_CHARS:
        return ""
    return "\n".join(f"[{ts // 60:02d}:{ts % 60:02d}] {line}" for ts, line in lines)


def process_one(video):
    """单个视频: 下载->OCR->存字幕->删临时文件; 返回 (bvid, status, elapsed)"""
    bvid, title = video["bvid"], video["title"]
    t0 = time.time()
    # 风控: 随机间隔
    time.sleep(random.uniform(3, 8))
    # 下载 (跳过已存在)
    mp4 = m.find_downloaded_mp4(bvid)
    consec_fail = 0
    for attempt in range(3):
        if not mp4:
            mp4 = m.download_video({"dl_retries": 2}, bvid)
        if mp4:
            break
        consec_fail += 1
        if consec_fail >= 3:
            time.sleep(120)   # 连续失败: 长暂停防风控
            consec_fail = 0
        time.sleep(15)
    if not mp4:
        return (bvid, title, "download-failed", time.time() - t0)
    # OCR
    try:
        text = ocr_video_fast(mp4, bvid, 0.5)
    except Exception as e:
        return (bvid, title, f"ocr-error:{str(e)[:80]}", time.time() - t0)
    # 存字幕 (空文件=无字幕标记)
    sub_path = os.path.join(SUBTITLE_DIR, f"{bvid}.txt")
    os.makedirs(SUBTITLE_DIR, exist_ok=True)
    with open(sub_path, "w", encoding="utf-8") as f:
        f.write(text)
    status = "ok" if text else "no-subtitle"
    # 清理临时文件 (内存/磁盘保护)
    try:
        os.remove(mp4)
        shutil_rmtree(os.path.join(FRAMES_DIR, bvid))
        shutil_rmtree(os.path.join(DOWNLOAD_DIR, bvid))
    except Exception:
        pass
    return (bvid, title, status, time.time() - t0)


def shutil_rmtree(p):
    import shutil
    if os.path.isdir(p):
        shutil.rmtree(p, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=2, help="并行worker数 (每worker约600MB内存)")
    ap.add_argument("--limit", type=int, default=0, help="只处理前N个(测试用)")
    ap.add_argument("--from-index", type=int, default=0, help="从第几个开始(旧的在前)")
    args = ap.parse_args()

    videos = fetch_all_videos()
    print(f"全部视频: {len(videos)} 条", flush=True)
    # 过滤已有字幕
    todo = [v for v in videos
            if not os.path.exists(os.path.join(SUBTITLE_DIR, f"{v['bvid']}.txt"))]
    print(f"待处理: {len(todo)} 条 (已有字幕 {len(videos) - len(todo)} 条)", flush=True)
    if args.limit:
        todo = todo[:args.limit]
    if args.from_index:
        todo = todo[args.from_index:]
    if not todo:
        print("全部完成, 无事可做", flush=True)
        return 0

    done = 0
    t_start = time.time()
    stats = {"ok": 0, "no-subtitle": 0, "download-failed": 0, "ocr-error": 0}
    print(f"开始批量处理: {len(todo)} 条, workers={args.workers}", flush=True)
    log_line(f"批量开始: {len(todo)} 条 workers={args.workers}")

    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(process_one, v): v for v in todo}
        for fut in as_completed(futs):
            v = futs[fut]
            try:
                bvid, title, status, elapsed = fut.result()
            except Exception as e:
                bvid, title, status, elapsed = v["bvid"], v["title"], f"crash:{str(e)[:60]}", 0
            done += 1
            kind = status.split(":")[0]
            stats[kind] = stats.get(kind, 0) + 1
            if status.startswith("download-failed"):
                with open(FAIL_FILE, "a", encoding="utf-8") as f:
                    f.write(f"{bvid}|{title}\n")
            eta = (time.time() - t_start) / done * (len(todo) - done) / 3600
            line = (f"[{done}/{len(todo)}] {bvid} {status} ({elapsed:.0f}s) "
                    f"ETA {eta:.1f}h 累计: " + " ".join(f"{k}={v2}" for k, v2 in stats.items()))
            print(line, flush=True)
            log_line(f"{line} | {title}")

    total = time.time() - t_start
    print(f"\n完成! 总耗时 {total / 3600:.1f} 小时, 统计: {stats}", flush=True)
    print(f"字幕目录: {SUBTITLE_DIR}", flush=True)
    if os.path.exists(FAIL_FILE):
        print(f"下载失败列表: {FAIL_FILE}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
