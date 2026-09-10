#!/usr/bin/env python3
"""诊断 bilidown dl 失败原因 (临时脚本, 用完即删)。

复刻 monitor.py 的调用环境, 原样跑一次下载, 不吞任何输出。
"""
import os
import shutil
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
WORKDIR = os.path.dirname(os.path.abspath(__file__))
BV = os.environ.get("DIAG_BV", "BV1SaYK6nEDY")


def hdr(t):
    print("\n" + "=" * 72)
    print(t)
    print("=" * 72)


hdr("1. 环境")
print("  cwd            :", os.getcwd())
print("  WORKDIR        :", WORKDIR)
print("  python         :", sys.executable, sys.version.split()[0])
print("  HOME           :", os.path.expanduser("~"))
print("  shutil.which(ffmpeg) :", shutil.which("ffmpeg"))
print("  shutil.which(ffprobe):", shutil.which("ffprobe"))

hdr("2. cookies 文件状态")
paths = [
    os.path.join(WORKDIR, "data", "cookies.txt"),
    os.path.join(os.path.expanduser("~"), ".cache", "bilibili-login-cookies.txt"),
]
for p in paths:
    if os.path.exists(p):
        sz = os.path.getsize(p)
        keys = []
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                parts = line.strip().split("\t")
                if len(parts) >= 7:
                    keys.append(parts[5])
        print("  %s" % p)
        print("     存在, %d 字节, cookie 键: %s" % (sz, keys))
        print("     含 SESSDATA: %s" % ("SESSDATA" in keys))
    else:
        print("  %s -> 不存在" % p)

hdr("3. bilidown 位置与版本")
py_script = os.path.join(WORKDIR, "_repo", "bin", "bilidown.py")
bash_script = os.path.join(WORKDIR, "_repo", "bin", "bilidown")
print("  bilidown.py  存在:", os.path.exists(py_script))
print("  bin/bilidown 存在:", os.path.exists(bash_script))
if os.path.exists(py_script):
    r = subprocess.run([sys.executable, py_script, "version"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    print("  version:", (r.stdout or "").strip(), "| rc:", r.returncode)
    print("  stderr :", (r.stderr or "").strip()[:200])

hdr("4. 原样复刻 monitor 的下载调用")
outdir = os.path.join(WORKDIR, "downloads", BV)
os.makedirs(outdir, exist_ok=True)
env = os.environ.copy()
env["PATH"] = os.pathsep.join([os.path.join(WORKDIR, "tools", "ffmpeg", "bin"),
                               env.get("PATH", "")])
cmd = [sys.executable, py_script, "dl", BV, "video", outdir, "mp4", "480", "1"]
print("  $", " ".join(cmd))
r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                   errors="replace", env=env, timeout=900)
print("\n  ---- rc =", r.returncode, "----")
print("  ---- STDOUT ----")
print(r.stdout or "(空)")
print("  ---- STDERR ----")
print(r.stderr or "(空)")
print("  ---- 结束 ----")

hdr("5. 产物")
found = []
for root, _, files in os.walk(os.path.join(WORKDIR, "downloads")):
    for f in files:
        p = os.path.join(root, f)
        found.append((os.path.getsize(p), p))
if found:
    for sz, p in found:
        print("  %8d  %s" % (sz, p))
else:
    print("  (无任何文件)")

hdr("6. 单独验证 ffmpeg 可用性")
for tool in ("ffmpeg", "ffprobe"):
    w = shutil.which(tool)
    if w:
        rr = subprocess.run([w, "-version"], capture_output=True, text=True, timeout=30)
        print("  %s -> %s" % (tool, (rr.stdout or "").splitlines()[0][:90]))
    else:
        print("  %s -> 未找到" % tool)
