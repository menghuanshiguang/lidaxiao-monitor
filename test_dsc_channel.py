#!/usr/bin/env python3
"""真实测试: 视频分析里 deepseek-chat-cli 这条兜底通道(调用仓库自带的 _call_dsc)。

monitor._call_dsc 会: 克隆/定位 dsc.py → 写 ~/.dsv_token → 每次全新 profile →
以 prompt 作为单个命令行参数调用 → 识别 "(超时未获取回答)" 为失败。
这里原样复用该函数, 逐步打印前置条件, 便于定位卡在哪一步。
一次性脚本, 跑完随 workflow 自删。
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import urllib.request

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

hdr("0. 环境")
print("  平台      :", sys.platform)
print("  cwd       :", os.getcwd())
print("  HOME      :", os.path.expanduser("~"))
print("  python    :", sys.executable, sys.version.split()[0])
mon.kill_stale() if hasattr(mon, "kill_stale") else None
for tool in ("msedge.exe", "chrome.exe"):
    print("  which %-11s:" % tool, shutil.which(tool))
for p in [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
          r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"]:
    print("  存在 %-62s %s" % (p, os.path.exists(p)))

hdr("1. DSV_TOKEN 是否有效(直接打官网接口)")
tok = (os.environ.get("DSV_TOKEN") or "").strip()
print("  token 长度:", len(tok))
if tok:
    try:
        req = urllib.request.Request(
            "https://chat.deepseek.com/api/v0/users/current",
            headers={"Authorization": "Bearer " + tok, "accept": "application/json",
                     "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120"})
        with urllib.request.urlopen(req, timeout=25) as r:
            d = json.loads(r.read())
        print("  接口返回 code =", d.get("code"), "| msg =", d.get("msg"))
        print("  ->", "token 有效 ✅" if d.get("code") == 0 else "token 无效/过期 ❌")
    except Exception as e:
        print("  请求异常:", type(e).__name__, str(e)[:200])
else:
    print("  ❌ 没有 DSV_TOKEN")

hdr("2. 克隆 deepseek-chat-cli(与 monitor 一致)")
cfg = mon.load_config(os.path.join(WORKDIR, "config.json"))
pcfg = dict(cfg["llm_cli"])
print("  provider 配置:", json.dumps(pcfg, ensure_ascii=False))
try:
    mon._clone_dsc(pcfg)
except Exception as e:
    print("  克隆异常:", e)
dsc_py = mon._find_dsc_path(pcfg)
print("  dsc.py:", dsc_py)

if dsc_py:
    # 打印关键实现, 确认拿到的是哪一版
    src = open(dsc_py, encoding="utf-8").read()
    print("  dsc.py 字节数:", len(src))
    checks = {
        "token 用 JSON 包装注入": "JSON.stringify({value:" in src,
        "发送键优先精确 token": "ds-button--primary" in src and "__inBtn" in src,
        "排除按钮内部嵌套层": "__inBtn(e)) continue" in src,
        "管道模式(无参数可读 stdin)": "if args and args[0] in (\"-h\", \"--help\")" in src,
        "tab_id 正则已修": "tab_id:\\\\s*\\\\d+\\\\s*$" in src or "\\\\s*tab_id:\\\\s*\\\\d+\\\\s*$" in src,
    }
    for k, v in checks.items():
        print("    %s %s" % ("OK  " if v else "缺失", k))
    r = subprocess.run([sys.executable, dsc_py, "version"], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=60)
    print("  dsc.py version ->", (r.stdout or r.stderr or "").strip()[:120])

hdr("3. 真实调用 _call_dsc(视频分析实际走的函数)")
prompt = ("你是简洁的中文助手, 只回答被问的内容。\n\n"
          "用一句话说明什么是A股, 不要展开。")
print("  prompt 长度:", len(prompt), "字符")
import time
t0 = time.time()
try:
    out = mon._call_dsc(pcfg, tok, prompt)
    dt = time.time() - t0
    print("  ✅ 成功 (%.1fs, %d 字)" % (dt, len(out)))
    print("  返回内容:", out[:400].replace("\n", " "))
    print()
    print("  结论: deepseek-chat-cli 通道可用 ✅")
except Exception as e:
    dt = time.time() - t0
    print("  ❌ 失败 (%.1fs)" % dt)
    print("  %s: %s" % (type(e).__name__, str(e)[:600]))
    print()
    print("  结论: 该通道仍不可用, 需继续兜底到 DeepSeek 官方 API")

hdr("4. dsc 的临时产物/日志")
for d in ("data/dsc_profiles",):
    p = os.path.join(WORKDIR, d)
    print("  %s 存在: %s" % (d, os.path.exists(p)))
tp = os.path.expanduser("~/.dsv_token")
print("  ~/.dsv_token 已写入:", os.path.exists(tp), "| 字节:", os.path.getsize(tp) if os.path.exists(tp) else 0)

hdr("测试结束")
