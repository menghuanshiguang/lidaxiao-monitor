#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对比不同请求方式拉 B站 直链的结果, 定位 bilidown 为什么 567/403"""
import sys
import urllib.request

import requests

BV = sys.argv[1] if len(sys.argv) > 1 else "BV1vusA6cENj"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

S = requests.Session()
S.headers.update({"User-Agent": UA, "Referer": "https://www.bilibili.com/"})
spi = S.get("https://api.bilibili.com/x/frontend/finger/spi", timeout=10).json()
S.cookies.set("buvid3", spi["data"]["b_3"], domain=".bilibili.com")
cid = S.get("https://api.bilibili.com/x/web-interface/view", params={"bvid": BV},
            timeout=15).json()["data"]["cid"]
j = S.get("https://api.bilibili.com/x/player/playurl",
          params={"bvid": BV, "cid": cid, "qn": 32, "fnval": 16,
                  "platform": "pc", "high_quality": 1}, timeout=15).json()
vids = ((j.get("data") or {}).get("dash") or {}).get("video") or []
url = vids[0]["baseUrl"]
print("host:", url.split("/")[2])

def show(name, fn):
    try:
        r = fn()
        code = getattr(r, "status", None) or getattr(r, "status_code", None)
        n = len(r.read(262144)) if hasattr(r, "read") else 0
        print(f"{name:24s} -> {code} {n}B")
    except Exception as e:
        print(f"{name:24s} -> EXC {type(e).__name__}: {e}")

# 1) requests 裸
def bare():
    return S.get(url, timeout=20, stream=True)
# 2) requests + Range
def rng():
    return S.get(url, headers={"Range": "bytes=0-262143"}, timeout=20, stream=True)
# 3) requests + Origin
def origin():
    return S.get(url, headers={"Origin": "https://www.bilibili.com"}, timeout=20, stream=True)
# 4) urllib 裸 (bilidown 风格)
def ur():
    return urllib.request.urlopen(urllib.request.Request(
        url, headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"}), timeout=20)
# 5) urllib + Range
def ur_rng():
    return urllib.request.urlopen(urllib.request.Request(
        url, headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/",
                      "Range": "bytes=0-262143"}), timeout=20)

for name, fn in [("requests bare", bare), ("requests +Range", rng),
                 ("requests +Origin", origin), ("urllib bare", ur),
                 ("urllib +Range", ur_rng)]:
    show(name, fn)
