#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成评测夹具: 下载一条李大霄视频 -> 抽取底部字幕带 -> RapidOCR 识别 -> subtitle.txt
用法: python bench/make_fixture.py [BV号]
"""
import json
import os
import subprocess
import sys

BV = sys.argv[1] if len(sys.argv) > 1 else "BV1vusA6cENj"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixture")
FRAMES = os.path.join(OUT, "frames")
os.makedirs(FRAMES, exist_ok=True)
URL = f"https://www.bilibili.com/video/{BV}"

COOKIES = ["--cookies", "data/cookies.txt"] if os.path.exists("data/cookies.txt") else []
print(f"[1/4] 取元信息 {BV} (cookies={'yes' if COOKIES else 'no'})", flush=True)


def run(args):
    p = subprocess.run(args, capture_output=True, text=True)
    if p.returncode != 0:
        print("STDOUT:", p.stdout[-3000:], flush=True)
        print("STDERR:", p.stderr[-3000:], flush=True)
        raise SystemExit(f"命令失败: {' '.join(args[:4])} rc={p.returncode}")
    return p


info = json.loads(run(["yt-dlp", "-J", "--no-warnings", *COOKIES, URL]).stdout)
title = info.get("title", "")
ts = int(info.get("timestamp") or 0)
dur = int(info.get("duration") or 0)
print(f"    {title} | {dur}s", flush=True)

print("[2/4] 下载 720P 以内视频", flush=True)
for old in os.listdir(OUT):
    if old.startswith("video."):
        os.remove(os.path.join(OUT, old))
subprocess.run(
    ["yt-dlp", "--no-warnings", "-f", "bv*[height<=720]+ba/b[height<=720]/b",
     "-o", os.path.join(OUT, "video.%(ext)s"), URL],
    check=True)

vids = [os.path.join(OUT, f) for f in os.listdir(OUT) if f.startswith("video.")]
video = vids[0]
print(f"    {video} {os.path.getsize(video)/1e6:.1f}MB", flush=True)

print("[3/4] 抽帧(1fps, 底部字幕带)", flush=True)
for f in os.listdir(FRAMES):
    os.remove(os.path.join(FRAMES, f))
subprocess.run(
    ["ffmpeg", "-y", "-loglevel", "error", "-i", video,
     "-vf", "fps=1,crop=iw:ih*0.28:0:ih*0.71", "-q:v", "3",
     os.path.join(FRAMES, "%05d.jpg")],
    check=True)
frames = sorted(os.listdir(FRAMES))
print(f"    {len(frames)} 帧", flush=True)

print("[4/4] OCR", flush=True)
from rapidocr_onnxruntime import RapidOCR  # noqa: E402

engine = RapidOCR()
lines = []
for i, fn in enumerate(frames):
    path = os.path.join(FRAMES, fn)
    try:
        res, _ = engine(path)
    except Exception:
        res = None
    if not res:
        continue
    parts = [t for _box, t, score in res if score >= 0.5 and t.strip()]
    if not parts:
        continue
    text = "".join(parts).strip()
    sec = i
    if lines and text == lines[-1][1]:
        continue
    lines.append((sec, text))
    if i % 100 == 0:
        print(f"    {i}/{len(frames)}", flush=True)

def mmss(s):
    return f"{s//60:02d}:{s%60:02d}"

with open(os.path.join(OUT, "subtitle.txt"), "w", encoding="utf-8") as f:
    for sec, text in lines:
        f.write(f"[{mmss(sec)}] {text}\n")
json.dump({"bvid": BV, "title": title, "timestamp": ts, "duration": dur,
           "dur_str": mmss(dur), "n_lines": len(lines)},
          open(os.path.join(OUT, "meta.json"), "w", encoding="utf-8"),
          ensure_ascii=False, indent=2)
print(f"OK: {len(lines)} 行 -> bench/fixture/subtitle.txt", flush=True)
print("--- 前 5 行 ---", flush=True)
print("\n".join(x[1] for x in lines[:5]), flush=True)
