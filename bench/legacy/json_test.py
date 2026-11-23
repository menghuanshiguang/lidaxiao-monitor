#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测本地 JSON 两步法: 抽取 -> 模板渲染, 并跑结构校验"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import monitor as m  # noqa: E402
from lidaxiao.analyze import validate_report  # noqa: E402

cfg = m.load_config()
cfg["llm_local"]["enabled"] = True
# 测试环境用本脚本起的 llama.cpp(8080), 覆盖生产配置里的 Ollama 地址
cfg["llm_local"]["base_url"] = os.environ.get("LOCAL_BASE_URL", "http://127.0.0.1:8080/v1")
cfg["llm_local"]["model"] = "local"
cfg["llm_local"]["enable_thinking"] = False
for k in ("llm", "llm_cli", "llm_fallback", "llm_free"):
    cfg.pop(k, None)

sub = open(os.path.join(HERE, "fixture", "subtitle.txt"), encoding="utf-8").read()
meta = json.load(open(os.path.join(HERE, "fixture", "meta.json"), encoding="utf-8"))
video = {"title": meta["title"], "pubdate": meta["timestamp"], "dur_str": meta["dur_str"]}

t0 = time.time()
out = m.analyze_subtitles(cfg, None, video, sub, "")
el = time.time() - t0
ok, miss = validate_report(out, sub)
print(f"=== 耗时 {el:.1f}s | 长度 {len(out)} | 校验 {ok} | 缺 {miss}")
print(out)
