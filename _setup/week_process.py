#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""处理指定日期范围内的视频(下载+OCR+AI分析+写报告), 用于周报/补全"""
import datetime
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import monitor as m

tz = datetime.timezone(datetime.timedelta(hours=8))


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    cutoff = datetime.datetime.now(tz) - datetime.timedelta(days=days)
    with open(os.path.join(m.WORKDIR, "data", "pubdates.json"), encoding="utf-8") as f:
        data = json.load(f)
    videos = [v for v in data if v["created"] >= cutoff.timestamp()]
    videos.sort(key=lambda v: v["created"])
    print(f"范围内 {len(videos)} 个视频", flush=True)

    cfg = m.load_config()
    api_key = m.load_llm_key()
    if not api_key:
        print("无 LLM 密钥 (OPENCODE_GO_API_KEY / DEEPSEEK_API_KEY)", file=sys.stderr)
        return 1

    lock = m.RunLock(os.path.join(m.WORKDIR, "data", "week_process.lock"))
    if not lock.acquire():
        return 0
    try:
        for i, v in enumerate(videos, 1):
            bvid = v["bvid"]
            # 已有非空字幕 + 已分析过 -> 跳过 (processed 中 ok/partial)
            done = m.processed_bvids()
            if bvid in done:
                print(f"[{i}/{len(videos)}] {bvid} 已处理, 跳过", flush=True)
                continue
            t0 = time.time()
            ok, rp, note = m.process_video(cfg, api_key, v)
            status = "ok" if ok else "error"
            m.mark_processed(bvid, v.get("title", ""), status)
            print(f"[{i}/{len(videos)}] {bvid} -> {status} ({note}) {time.time()-t0:.0f}s 报告: {rp}", flush=True)
    finally:
        lock.release()
    print("完成", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
