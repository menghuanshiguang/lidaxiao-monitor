#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
拉取李大霄历史视频发布时间 (B站 arc/search API, wbi 签名 + 登录cookies)
输出: data/pubdates.json  [{bvid, title, created, length}]
用法: python _setup/fetch_pubdates.py [条数=400]
"""
import hashlib
import json
import os
import sys
import time
import urllib.parse
import urllib.request

UID = "2137589551"
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
MIXIN = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
         33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
         26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
         20, 34, 44, 52]


def load_cookies():
    for p in [os.path.join(BASE, "data", "cookies.txt"),
              os.path.join(os.path.expanduser("~"), ".cache",
                           "bilibili-login-cookies.txt")]:
        if os.path.exists(p):
            pairs = []
            for line in open(p, encoding="utf-8", errors="replace"):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 7:
                    pairs.append(f"{parts[5]}={parts[6]}")
            if pairs:
                return "; ".join(pairs)
    return ""


def api(url, ck, referer=None):
    h = {"User-Agent": UA, "Cookie": ck}
    if referer:
        h["Referer"] = referer
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def get_wbi_key(ck):
    d = api("https://api.bilibili.com/x/web-interface/nav", ck)
    img = d["data"]["wbi_img"]["img_url"].split("/")[-1].split(".")[0]
    sub = d["data"]["wbi_img"]["sub_url"].split("/")[-1].split(".")[0]
    key = img + sub
    mixin_key = "".join(key[i] for i in MIXIN)[:32]
    return mixin_key


def main():
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 400
    ck = load_cookies()
    if not ck:
        print("无 cookies, 退出", file=sys.stderr)
        return 1
    mixin_key = get_wbi_key(ck)
    out = []
    page = 1
    while len(out) < count and page <= 40:
        params = {"mid": UID, "ps": "50", "pn": str(page), "order": "pubdate",
                  "wts": int(time.time())}
        items = sorted(params.items())
        query = urllib.parse.urlencode(items)
        params["w_rid"] = hashlib.md5((query + mixin_key).encode()).hexdigest()
        url = ("https://api.bilibili.com/x/space/wbi/arc/search?"
               + urllib.parse.urlencode(params))
        d = api(url, ck, referer=f"https://space.bilibili.com/{UID}")
        if d.get("code") != 0:
            print(f"API错误 code={d.get('code')} {d.get('message', '')}", file=sys.stderr)
            break
        vlist = d["data"]["list"]["vlist"]
        if not vlist:
            break
        for v in vlist:
            out.append({"bvid": v["bvid"], "title": v["title"],
                        "created": v["created"], "length": v["length"]})
        total = d["data"]["page"]["count"]
        print(f"页{page}: 累计 {len(out)}/{total}", file=sys.stderr)
        page += 1
        time.sleep(1.2)  # 风控礼貌
    dst = os.path.join(BASE, "data", "pubdates.json")
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"已保存 {len(out)} 条到 {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
