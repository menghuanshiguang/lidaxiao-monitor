#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清空失败/超时/今日分析状态, 让下次 Actions 自动重跑。

- 扫描 docs/reports/*.md, 找出包含 "AI分析失败" 或 "(超时未获取回答)" 的 BV
- 同时按北京时间"今天"的发布日期, 把今天的 BV 一并清掉(即使日报已删除)
- 从 data/processed.txt 移除这些 BV 以及所有 partial/error 状态行
- 把 data/last_bvid.txt 置为哨兵, 让最近视频重新扫描
"""
import datetime
import glob
import json
import os
import re
import sys

WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(WORKDIR)

# Windows runner 默认 cp1252, 必须切 UTF-8 才能打印中文
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

CN_TZ = datetime.timezone(datetime.timedelta(hours=8))
SECTION_RE = re.compile(r"^##\s*视频\d+:.*?\(?(BV\w+)\)?\s*$")
MARKERS = ("AI分析失败", "超时未获取回答", "未获取回答")


def extract_failed_bvids():
    bvids = set()
    for f in glob.glob(os.path.join("docs", "reports", "*.md")):
        try:
            with open(f, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        except Exception as e:
            print(f"⚠️ 读取 {f} 失败: {e}", file=sys.stderr)
            continue
        cur = None
        for ln in lines:
            m = SECTION_RE.match(ln.strip())
            if m:
                cur = m.group(1)
                continue
            if cur and any(mk in ln for mk in MARKERS):
                bvids.add(cur)
    return bvids


def today_bvids():
    """按北京时间今天的发布日期, 返回今天的 BV 集合"""
    today = datetime.datetime.now(CN_TZ).date()
    bvids = set()
    for f in ("data/pubdates.json", "data/all_videos.json"):
        if not os.path.exists(f):
            continue
        try:
            with open(f, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as e:
            print(f"⚠️ 读取 {f} 失败: {e}", file=sys.stderr)
            continue
        for v in data:
            ts = v.get("created") or v.get("pubdate")
            if ts:
                d = datetime.datetime.fromtimestamp(ts, CN_TZ).date()
                if d == today:
                    bvids.add(v.get("bvid"))
    return bvids


def main():
    failed = extract_failed_bvids()
    today = today_bvids()
    targets = failed | today
    print(f"失败/超时 BV: {len(failed)} -> {sorted(failed)}")
    print(f"今日 BV: {len(today)} -> {sorted(today)}")
    print(f"待清理 BV 总数: {len(targets)}")

    proc = os.path.join("data", "processed.txt")
    if os.path.exists(proc):
        with open(proc, encoding="utf-8") as f:
            lines = [ln.rstrip("\n") for ln in f if ln.strip()]
        keep, removed = [], []
        for ln in lines:
            parts = ln.split("|")
            bvid = parts[0] if parts else ""
            status = parts[2] if len(parts) > 2 else ""
            if bvid in targets or status in ("partial", "error"):
                removed.append(ln)
            else:
                keep.append(ln)
        with open(proc, "w", encoding="utf-8") as f:
            f.write("\n".join(keep) + ("\n" if keep else ""))
        print(f"processed.txt: 移除 {len(removed)} 条, 保留 {len(keep)} 条")
    else:
        print("processed.txt 不存在, 跳过")

    with open(os.path.join("data", "last_bvid.txt"), "w", encoding="utf-8") as f:
        f.write("BV0reset")
    print("last_bvid -> BV0reset")


if __name__ == "__main__":
    main()
