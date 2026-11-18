#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用与生产完全一致的提示词, 打本地 llama-server (OpenAI 兼容), 输出报告 + 耗时
用法: python bench/run_bench.py --tag qwen25 --base http://127.0.0.1:8080/v1 --out bench/out/qwen25.md [--no-think]
"""
import argparse
import glob
import json
import os
import sys
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import monitor as m  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--tag", required=True)
ap.add_argument("--base", default="http://127.0.0.1:8080/v1")
ap.add_argument("--model", default="local")
ap.add_argument("--out", required=True)
ap.add_argument("--no-think", action="store_true")
ap.add_argument("--max-tokens", type=int, default=3000)
ap.add_argument("--temperature", type=float, default=0.3)
args = ap.parse_args()

fx = os.path.join(HERE, "fixture")
subtitle = open(os.path.join(fx, "subtitle.txt"), encoding="utf-8").read()
meta = json.load(open(os.path.join(fx, "meta.json"), encoding="utf-8"))

# 历史报告: 取 docs/reports 里最新的 5 篇
files = [f for f in sorted(glob.glob(os.path.join(ROOT, "docs", "reports", "*.md")))
         if not os.path.basename(f).startswith(("index", "latest", "weekly"))]
history = "\n\n".join(open(f, encoding="utf-8").read() for f in files[-5:])[-6000:]
print(f"history: {len(history)} chars / {len(files[-5:])} 篇; subtitle: {len(subtitle)} chars", flush=True)

import datetime  # noqa: E402
date = datetime.datetime.fromtimestamp(meta["timestamp"], m.CN_TZ).strftime("%Y-%m-%d")
ending_block = ""
user = f"""【视频信息】
日期: {date}
标题: {meta['title']}
时长: {meta['dur_str']}

【字幕文本】
{subtitle[:12000]}
{ending_block}
【历史报告(最近5天, 供观点连续性对比)】
{history or "(无历史报告)"}

请按模板输出分析。"""

system = m.ANALYZE_SYSTEM
if args.no_think:
    system = system + "\n/no_think"

payload = {
    "model": args.model,
    "messages": [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ],
    "max_tokens": args.max_tokens,
    "temperature": args.temperature,
    "stream": False,
}

t0 = time.time()
r = requests.post(args.base.rstrip("/") + "/chat/completions", json=payload, timeout=1800)
elapsed = time.time() - t0
data = r.json()
try:
    text = data["choices"][0]["message"]["content"]
except Exception:
    text = f"[ERROR] HTTP {r.status_code}: {json.dumps(data, ensure_ascii=False)[:2000]}"
usage = data.get("usage", {}) or {}
pt = usage.get("prompt_tokens", 0)
ct = usage.get("completion_tokens", 0)

os.makedirs(os.path.dirname(args.out), exist_ok=True)
with open(args.out, "w", encoding="utf-8") as f:
    f.write(text)
print(f"=== {args.tag} | {elapsed:.1f}s | prompt {pt} tok | completion {ct} tok "
      f"| {ct/max(elapsed,1e-9):.1f} tok/s | {len(text)} chars ===", flush=True)
print(text, flush=True)
json.dump({"tag": args.tag, "elapsed": elapsed, "prompt_tokens": pt,
           "completion_tokens": ct, "chars": len(text), "server": args.base},
          open(args.out + ".meta.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
