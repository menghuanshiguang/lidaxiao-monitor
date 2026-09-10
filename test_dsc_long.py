#!/usr/bin/env python3
"""真实长度 prompt 下的 dsc 通道测试 + 输出格式检查。

监控里 build_dsc_prompt 会拼出 3000-6000 字的 prompt(字幕上限 12000 字),
短 prompt 测不出问题, 这里用接近真实的长度, 并检查返回是否保持列表结构。
"""
import importlib.util
import json
import os
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
WORKDIR = os.path.dirname(os.path.abspath(__file__))


def hdr(t):
    print("\n" + "=" * 74)
    print(t)
    print("=" * 74)


spec = importlib.util.spec_from_file_location("mon", os.path.join(WORKDIR, "monitor.py"))
mon = importlib.util.module_from_spec(spec)
sys.modules["mon"] = mon
spec.loader.exec_module(mon)

tok = (os.environ.get("DSV_TOKEN") or "").strip()
cfg = mon.load_config(os.path.join(WORKDIR, "config.json"))
pcfg = dict(cfg["llm_cli"])

hdr("1. 用真实长度的 prompt(仿 build_dsc_prompt)")
# 造一段接近真实字幕长度的文本
sub = "，".join(["他说这个市场有巨大的风险需要警惕高估值的品种而红利央企银行非银金融仍然是中流砥柱"] * 60)
video = {"title": "警惕次新股解禁的风险", "dur_str": "04:39", "pubdate": 1786000000}
history = "（历史报告）" + "市场定性：防御。观点连续性：延续。" * 40
ending = {"text": "【片尾画面】地球/星球 → 地球顶\n【片尾画面暗示】地球/星球 → 🔴高位风险"}
prompt = mon.build_dsc_prompt(video, sub, history, ending)
print("  prompt 长度:", len(prompt), "字符")
print("  前 200 字:", prompt[:200].replace("\n", " "))
print("  末 300 字:", prompt[-300:].replace("\n", " "))
open(os.path.join(WORKDIR, "_prompt_used.txt"), "w", encoding="utf-8").write(prompt)

hdr("2. 真实调用 _call_dsc(长 prompt)")
t0 = time.time()
try:
    out = mon._call_dsc(pcfg, tok, prompt)
    dt = time.time() - t0
    print("  ✅ 成功 (%.1fs)" % dt)
    print("  返回长度: %d 字符" % len(out))
    print("=" * 74)
    print(out[:2500])
    print("=" * 74)
    # 格式检查
    lines = [l for l in out.splitlines() if l.strip()]
    numbered = [l for l in lines if l.strip()[:2].rstrip(".").isdigit()]
    dashed = [l for l in lines if l.strip().startswith("-")]
    sections = [l for l in lines if l.strip().startswith("【")]
    print()
    print("  段落标题数(【】):", len(sections))
    print("  编号行数(1. 2. ...):", len(numbered))
    print("  项目符号行数(- ):", len(dashed))
    print("  是否含 片尾图形: ->", "片尾图形" in out)
    print("  是否含 片尾:    ->", "片尾:" in out)
    print("  结尾是否为免责声明 ->", out.strip().endswith("不构成投资建议"))
except Exception as e:
    dt = time.time() - t0
    print("  ❌ 失败 (%.1fs): %s: %s" % (dt, type(e).__name__, str(e)[:400]))

hdr("测试结束")
