#!/usr/bin/env python3
"""诊断 bilidown 下载失败: 抓出被吞掉的真实异常 + CDN 的真实 HTTP 状态。

一次性脚本, 随 workflow 一起自删。不改动 monitor.py / config.json 等任何现有文件。
"""
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WORKDIR = os.path.dirname(os.path.abspath(__file__))
BV = os.environ.get("DIAG_BV", "BV1SaYK6nEDY")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def hdr(t):
    print("\n" + "=" * 74)
    print(t)
    print("=" * 74)


def cookie_paths():
    return [os.path.join(WORKDIR, "data", "cookies.txt"),
            os.path.join(os.path.expanduser("~"), ".cache", "bilibili-login-cookies.txt")]


def read_cookies():
    ck = {}
    used = None
    for p in cookie_paths():
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split("\t")
                    if len(parts) >= 7:
                        ck[parts[5]] = parts[6]
            if ck:
                used = p
                break
    return ck, used


hdr("0. 运行环境 (与 monitor.yml 对齐)")
print("  cwd        :", os.getcwd())
print("  HOME       :", os.path.expanduser("~"))
print("  ffmpeg     :", shutil.which("ffmpeg"))
print("  ffprobe    :", shutil.which("ffprobe"))
print("  bilidown.py:", os.path.exists(os.path.join(WORKDIR, "_repo", "bin", "bilidown.py")))

hdr("1. cookies 状态")
ck, used = read_cookies()
print("  实际读取到 cookies 的文件:", used)
print("  cookie 键:", sorted(ck.keys()))
print("  含 SESSDATA:", "SESSDATA" in ck)
print("  含 bili_jct:", "bili_jct" in ck)
for p in cookie_paths():
    print("  %s -> %s" % (p, ("%d 字节" % os.path.getsize(p)) if os.path.exists(p) else "不存在"))

ckstr = "; ".join("%s=%s" % (k, v) for k, v in ck.items())

hdr("2. 复刻 monitor 的下载调用 (原样)")
outdir = os.path.join(WORKDIR, "downloads", BV)
os.makedirs(outdir, exist_ok=True)
env = os.environ.copy()
env["PATH"] = os.pathsep.join([os.path.join(WORKDIR, "tools", "ffmpeg", "bin"),
                               env.get("PATH", "")])
cmd = [sys.executable, os.path.join(WORKDIR, "_repo", "bin", "bilidown.py"),
       "dl", BV, "video", outdir, "mp4", "480", "1"]
print("  $", " ".join(cmd))
r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                   errors="replace", env=env, timeout=900)
print("\n  ---- rc = %s ----" % r.returncode)
print("  ---- STDOUT ----"); print(r.stdout or "(空)")
print("  ---- STDERR ----"); print(r.stderr or "(空)")
print("  ---- 结束 ----")

hdr("3. 关键: 直接探 CDN 播放地址的真实 HTTP 状态 (诊断被吞掉的异常)")


def api(url, params, cookies):
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request("%s?%s" % (url, qs), headers={
        "User-Agent": UA, "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com", "Cookie": cookies,
    })
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return e.code, {"__httperror_body": e.read().decode("utf-8", "replace")[:300]}
    except Exception as e:
        return None, {"__error": "%s: %s" % (type(e).__name__, e)}


import urllib.parse  # noqa: E402

# 分P -> cid
st, d = api("https://api.bilibili.com/x/player/pagelist", {"bvid": BV}, ckstr)
print("  [pagelist] HTTP=%s code=%s" % (st, d.get("code")))
pages = d.get("data") or []
cid = pages[0].get("cid") if pages else None
print("  cid =", cid)

st2, d2 = api("https://api.bilibili.com/x/player/playurl",
              {"bvid": BV, "cid": cid, "qn": 80, "fnval": 16, "fourk": 1}, ckstr)
print("  [playurl ] HTTP=%s code=%s msg=%s" % (st2, d2.get("code"), d2.get("message")))
dash = ((d2.get("data") or {}).get("dash") or {})
vids = dash.get("video") or []
auds = dash.get("audio") or []
print("  可用视频流 %d 个, 音频流 %d 个" % (len(vids), len(auds)))
if vids:
    print("  视频流分辨率:", [v.get("id") for v in vids])

# 真去拉一小段, 抓真实状态码
for label, url in (([("视频流", vids[0]["baseUrl"])] if vids else []) +
                   ([("音频流", auds[0]["baseUrl"])] if auds else [])):
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Referer": "https://www.bilibili.com/"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            chunk = resp.read(65536)
            print("  [%s] HTTP=%s 读到 %d 字节 -> 可下载 ✅" % (label, resp.status, len(chunk)))
    except urllib.error.HTTPError as e:
        print("  [%s] HTTP=%s 原因=%r -> 被拒 ❌" % (label, e.code, e.reason))
        try:
            print("       响应体:", e.read().decode("utf-8", "replace")[:200])
        except Exception:
            pass
    except Exception as e:
        print("  [%s] 异常 %s: %s ❌" % (label, type(e).__name__, str(e)[:200]))

hdr("4. 对照: 纯游客(不带任何 cookies)能否拿到播放地址")
st3, d3 = api("https://api.bilibili.com/x/player/playurl",
              {"bvid": BV, "cid": cid, "qn": 80, "fnval": 16, "fourk": 1}, "")
print("  [游客 playurl] HTTP=%s code=%s msg=%s" % (st3, d3.get("code"), d3.get("message")))
gv = (((d3.get("data") or {}).get("dash") or {}).get("video") or [])
if gv:
    req = urllib.request.Request(gv[0]["baseUrl"], headers={
        "User-Agent": UA, "Referer": "https://www.bilibili.com/"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print("  [游客 视频流] HTTP=%s 读到 %d 字节 ✅" % (resp.status, len(resp.read(65536))))
    except urllib.error.HTTPError as e:
        print("  [游客 视频流] HTTP=%s 原因=%r ❌" % (e.code, e.reason))
    except Exception as e:
        print("  [游客 视频流] 异常 %s ❌" % type(e).__name__)

hdr("诊断结束")
