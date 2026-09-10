#!/usr/bin/env python3
"""真实验证: 用仓库自己的代码 + 新提示词, 对最新视频跑一次片尾画面识别。

会真的下载视频、抽片尾帧、调用视觉模型, 然后打印新格式输出与报告渲染结果。
一次性脚本, 跑完随 workflow 自删。
"""
import importlib.util
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
WORKDIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, WORKDIR)


def hdr(t):
    print("\n" + "=" * 74)
    print(t)
    print("=" * 74)


spec = importlib.util.spec_from_file_location("mon", os.path.join(WORKDIR, "monitor.py"))
mon = importlib.util.module_from_spec(spec)
sys.modules["mon"] = mon
spec.loader.exec_module(mon)

BV = os.environ.get("DIAG_BV", "BV1jBYN6tEcf")

hdr("0. 提示词版本与结构")
print("  ENDING_PROMPT_VER =", mon.ENDING_PROMPT_VER)
for ln in mon.ENDING_PROMPT.splitlines():
    s = ln.strip()
    if s.startswith("【"):
        print("    ", s.split("】")[0] + "】")

hdr("1. 下载视频")
cfg = mon.load_config(os.path.join(WORKDIR, "config.json"))
mp4 = mon.download_video(cfg, BV)
print("  mp4 =", mp4)
if not mp4:
    print("  ❌ 下载失败, 无法继续")
    sys.exit(1)

hdr("2. 片尾画面识别(新提示词, 绕过缓存)")
rec = mon.read_ending_hint(cfg, {"bvid": BV, "title": "", "pubdate": 0}, mp4, force=True)
if not rec:
    print("  ❌ 识别失败")
    sys.exit(1)
print("  provider =", rec.get("provider"), "| model =", rec.get("model"),
      "| ver =", rec.get("ver"), "| frames =", rec.get("frames"))

hdr("3. 原始输出(模型原始返回)")
print(rec.get("text", ""))

hdr("4. 报告渲染结果 (ending_md)")
print(mon.ending_md(rec))

hdr("5. 检查项")
text = rec.get("text", "")
lines = [l.strip() for l in text.splitlines() if l.strip()]
has_line = any(l.startswith("【片尾画面暗示】") for l in lines)
print("  行数(非空)            :", len(lines))
print("  含【片尾画面暗示】      :", has_line)
metaphor = ""
for l in lines:
    if l.startswith("【片尾画面暗示】"):
        metaphor = l.replace("【片尾画面暗示】", "").strip()
print("  画面暗示内容           :", metaphor[:160])
md = mon.ending_md(rec)
print("  渲染后含置顶加粗行      :", "**🎯 画面图形暗示:" in md or "(该行为'无', 故不置顶)")
print("  方向标记出现           :",
      any(k in metaphor for k in ("🔴", "🟢", "⚠️")) or "(无图形)")

hdr("验证结束")
