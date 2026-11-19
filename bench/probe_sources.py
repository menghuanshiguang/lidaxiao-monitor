#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B站取流多源探测: 枚举 platform/fnval 组合, 逐个 Range 试下载, 报告哪条还活着"""
import json
import sys

import requests

BV = sys.argv[1] if len(sys.argv) > 1 else "BV1vusA6cENj"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
S = requests.Session()
S.headers.update({"User-Agent": UA, "Referer": "https://www.bilibili.com/",
                  "Origin": "https://www.bilibili.com"})

# 拿 buvid3, 减少风控
try:
    spi = S.get("https://api.bilibili.com/x/frontend/finger/spi", timeout=10).json()
    b3 = spi.get("data", {}).get("b_3")
    if b3:
        S.cookies.set("buvid3", b3, domain=".bilibili.com")
        print("buvid3 ok")
except Exception as e:
    print("buvid fail", e)

view = S.get("https://api.bilibili.com/x/web-interface/view",
             params={"bvid": BV}, timeout=15).json()
cid = view["data"]["cid"]
title = view["data"]["title"]
print(f"{BV} | {title} | cid={cid}")

combos = [
    ("html5", 1), ("html5", 16), ("pc", 16), ("pc", 4048), ("android", 16),
]
tested = 0
for platform, fnval in combos:
    params = {"bvid": BV, "cid": cid, "qn": 32, "fnval": fnval,
              "fourk": 1, "platform": platform, "high_quality": 1}
    try:
        j = S.get("https://api.bilibili.com/x/player/playurl", params=params,
                  timeout=15).json()
    except Exception as e:
        print(f"[{platform}/{fnval}] api ERR {e}")
        continue
    code = j.get("code")
    d = j.get("data") or {}
    urls = []
    for u in d.get("durl") or []:
        urls.append(("durl", u.get("url")))
    for v in (d.get("dash") or {}).get("video") or []:
        urls.append(("dash", v.get("baseUrl") or v.get("base_url")))
    for kind, url in urls[:3]:
        tested += 1
        host = url.split("/")[2] if url and "//" in url else "?"
        try:
            r = S.get(url, headers={"Range": "bytes=0-262143"}, timeout=20)
            ok = r.status_code in (200, 206) and len(r.content) > 1024
            print(f"[{platform}/{fnval}] {kind} {host} -> {r.status_code} "
                  f"{len(r.content)}B {'✅' if ok else '❌'}")
            if ok:
                print("SUCCESS_URL=", url[:120])
        except Exception as e:
            print(f"[{platform}/{fnval}] {kind} {host} -> EXC {e}")
    if not urls:
        print(f"[{platform}/{fnval}] code={code} msg={j.get('message')} 无URL")
print("tested", tested)
