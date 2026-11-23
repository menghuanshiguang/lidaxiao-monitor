#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用本地 VLM 跑生产同款片尾识别提示词"""
import base64
import glob
import json
import os
import sys
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from lidaxiao.prompts import ENDING_PROMPT  # noqa: E402

base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080/v1"
out = sys.argv[2] if len(sys.argv) > 2 else "bench/out/vision.md"

frames = sorted(glob.glob(os.path.join(HERE, "visionset", "*.jpg")))
content = [{"type": "text", "text": ENDING_PROMPT}]
for f in frames:
    b = base64.b64encode(open(f, "rb").read()).decode()
    content.append({"type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b}"}})

payload = {"model": "local", "messages": [{"role": "user", "content": content}],
           "max_tokens": 900, "temperature": 0.2, "stream": False}

t0 = time.time()
r = requests.post(base.rstrip("/") + "/chat/completions", json=payload, timeout=3600)
el = time.time() - t0
data = r.json()
msg = (data.get("choices") or [{}])[0].get("message") or {}
text = msg.get("content") or ""
if not text.strip():
    text = "[EMPTY] " + (msg.get("reasoning_content") or "")[:1500]
u = data.get("usage", {}) or {}
os.makedirs(os.path.dirname(out), exist_ok=True)
open(out, "w", encoding="utf-8").write(text)
print(f"=== {el:.1f}s | prompt {u.get('prompt_tokens')} tok | out {u.get('completion_tokens')} tok ===")
print(text)
json.dump({"elapsed": el, "usage": u}, open(out + ".meta.json", "w"), ensure_ascii=False)
