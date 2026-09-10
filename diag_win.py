#!/usr/bin/env python3
"""Windows 专用诊断: 复刻 monitor 的下载调用, 抓全输出。

与 diag_dl.py 的区别: 不预先写 cookies, 而是分别测「有 cookies」和「纯游客」两种,
以便判断到底哪种在 Windows runner 上会失败。
"""
import os
import shutil
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WORKDIR = os.path.dirname(os.path.abspath(__file__))
BV = os.environ.get("DIAG_BV", "BV1SaYK6nEDY")
BILIDOWN = os.path.join(WORKDIR, "_repo", "bin", "bilidown.py")


def hdr(t):
    print("\n" + "=" * 74)
    print(t)
    print("=" * 74)


def run(args, note):
    """完全复刻 monitor.run_bilidown 的调用方式"""
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join([os.path.join(WORKDIR, "tools", "ffmpeg", "bin"),
                                   env.get("PATH", "")])
    cmd = [sys.executable, BILIDOWN] + args
    print("\n  [%s]" % note)
    print("  $", " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env, timeout=900)
    print("  ---- rc = %s ----" % r.returncode)
    print("  ---- STDOUT ----")
    print(r.stdout or "(空)")
    print("  ---- STDERR ----")
    print(r.stderr or "(空)")
    return r.returncode


hdr("0. 环境")
print("  平台        :", sys.platform)
print("  cwd         :", os.getcwd())
print("  HOME        :", os.path.expanduser("~"))
print("  ffmpeg      :", shutil.which("ffmpeg"))
print("  ffprobe     :", shutil.which("ffprobe"))
print("  bilidown.py :", os.path.exists(BILIDOWN))
if shutil.which("ffmpeg"):
    rr = subprocess.run([shutil.which("ffmpeg"), "-version"], capture_output=True, text=True)
    print("  ffmpeg 版本 :", (rr.stdout or "").splitlines()[0][:80] if rr.stdout else "(无输出)")

hdr("1. cookies 文件状态")
paths = [os.path.join(WORKDIR, "data", "cookies.txt"),
         os.path.join(os.path.expanduser("~"), ".cache", "bilibili-login-cookies.txt")]
for p in paths:
    print("  %s -> %s" % (p, ("%d 字节" % os.path.getsize(p)) if os.path.exists(p) else "不存在"))

hdr("2. 首次下载(带 cookies, 与 monitor 完全一致)")
outdir = os.path.join(WORKDIR, "downloads", BV)
os.makedirs(outdir, exist_ok=True)
rc1 = run(["dl", BV, "video", outdir, "mp4", "480", "1"], "带 cookies 第一次")

hdr("3. 若失败, 立刻重试一次(看是否偶发)")
rc2 = None
if rc1 != 0:
    rc2 = run(["dl", BV, "video", outdir, "mp4", "480", "1"], "带 cookies 第二次")

hdr("4. 纯游客模式(把 cookies 文件全部挪走)")
moved = []
for p in paths:
    if os.path.exists(p):
        bak = p + ".bak"
        try:
            shutil.move(p, bak)
            moved.append((p, bak))
            print("  已挪开:", p)
        except Exception as e:
            print("  挪开失败:", p, e)
try:
    outdir2 = os.path.join(WORKDIR, "downloads", BV + "_guest")
    os.makedirs(outdir2, exist_ok=True)
    rc3 = run(["dl", BV, "video", outdir2, "mp4", "480", "1"], "纯游客")
finally:
    for p, bak in moved:
        try:
            shutil.move(bak, p)
        except Exception:
            pass

hdr("5. 产物")
for root, _, files in os.walk(os.path.join(WORKDIR, "downloads")):
    for f in files:
        p = os.path.join(root, f)
        print("  %10d  %s" % (os.path.getsize(p), p))

hdr("6. 结论")
print("  带 cookies 第一次 rc =", rc1)
print("  带 cookies 第二次 rc =", rc2)
print("  纯游客     rc =", rc3)
print("  cookies 文件在下载前是否存在:", all(os.path.exists(p) for p in paths) or "部分存在")
