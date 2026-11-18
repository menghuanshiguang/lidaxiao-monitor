#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地模型链路自检: 只走 local provider, 用真实字幕跑一遍 analyze_subtitles"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import monitor as m  # noqa: E402

cfg = m.load_config()
cfg["llm_local"]["enabled"] = True
for k in ("llm", "llm_cli", "llm_fallback", "llm_free"):
    cfg.pop(k, None)          # 只测本地, 不许回退

sub = open(os.path.join(HERE, "fixture", "subtitle.txt"), encoding="utf-8").read()
meta = json.load(open(os.path.join(HERE, "fixture", "meta.json"), encoding="utf-8"))
video = {"title": meta["title"], "pubdate": meta["timestamp"], "dur_str": meta["dur_str"]}

alive = m._local_llm_alive(cfg["llm_local"])
print(f"本地服务探活: {alive} @ {cfg['llm_local']['base_url']}")
if not alive:
    raise SystemExit("llama-server 未就绪")

out = m.analyze_subtitles(cfg, None, video, sub, "")
print("=" * 40)
print(out)
ok, miss = m.validate_report(out)
print("=" * 40)
print(f"结构校验: {ok} | 缺: {miss}")
raise SystemExit(0 if ok else 2)
