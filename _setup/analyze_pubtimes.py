#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
分析李大霄视频发布时间规律 -> 输出建议检查时间
用法: python _setup/analyze_pubtimes.py
"""
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CN = timezone(timedelta(hours=8))


def main():
    with open(os.path.join(BASE, "data", "pubdates.json"), encoding="utf-8") as f:
        data = json.load(f)
    data.sort(key=lambda v: v["created"])  # 旧 -> 新
    n = len(data)
    first = datetime.fromtimestamp(data[0]["created"], CN)
    last = datetime.fromtimestamp(data[-1]["created"], CN)
    span_days = (last - first).days + 1
    print(f"样本: {n} 条, 时间跨度 {first:%Y-%m-%d} ~ {last:%Y-%m-%d} ({span_days}天)")

    # 小时分布
    hour_cnt = Counter()
    dow_cnt = Counter()
    for v in data:
        dt = datetime.fromtimestamp(v["created"], CN)
        hour_cnt[dt.hour] += 1
        dow_cnt[dt.weekday()] += 1
    DOW = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

    print("\n=== 按小时分布 (发布数 / 占比) ===")
    total = n
    for h in range(24):
        c = hour_cnt.get(h, 0)
        bar = "#" * round(c / max(1, total) * 100)
        print(f"{h:02d}时: {c:4d} ({c/total*100:4.1f}%) {bar}")

    print("\n=== 按星期分布 ===")
    for d in range(7):
        c = dow_cnt.get(d, 0)
        print(f"{DOW[d]}: {c:4d} ({c/total*100:4.1f}%)")

    # 每天发布条数
    per_day = Counter()
    for v in data:
        per_day[datetime.fromtimestamp(v["created"], CN).date()] += 1
    days = list(per_day.values())
    from statistics import mean, median
    print(f"\n=== 每日发布条数 === 均值{mean(days):.2f} 中位{median(days)} "
          f"最多{max(days)} (有视频天数{len(days)})")

    # 发布间隔
    gaps = []
    for a, b in zip(data[:-1], data[1:]):
        gaps.append((b["created"] - a["created"]) / 3600)
    gaps = [g for g in gaps if g > 0]
    print(f"\n=== 发布间隔(小时) === 均值{mean(gaps):.1f} 中位{median(gaps):.1f} "
          f"最短{min(gaps):.2f} 最长{max(gaps):.0f}")
    gap_days = [g / 24 for g in gaps]
    lt24 = sum(1 for g in gaps if g < 24) / len(gaps) * 100
    lt48 = sum(1 for g in gaps if g < 48) / len(gaps) * 100
    print(f"间隔<24h: {lt24:.0f}%  <48h: {lt48:.0f}%")

    # 高峰时段识别: 累计占比 > 某阈值的连续时段
    print("\n=== 时段聚类 (高峰时段) ===")
    hours_sorted = sorted(hour_cnt.items(), key=lambda x: -x[1])
    cum = 0
    peaks = []
    for h, c in hours_sorted:
        cum += c
        peaks.append(h)
        if cum / total >= 0.80:
            break
    peaks = sorted(peaks)
    # 聚成连续段
    segs = []
    for h in peaks:
        if segs and h == segs[-1][-1] + 1:
            segs[-1].append(h)
        else:
            segs.append([h])
    for s in segs:
        cnt = sum(hour_cnt.get(h, 0) for h in s)
        print(f"{s[0]:02d}:00-{s[-1]+1:02d}:00  共{cnt}条 ({cnt/total*100:.0f}%)")

    # 建议检查时间: 高峰时段之后 30 分钟
    print("\n=== 建议定时检查时间 ===")
    checks = []
    for s in segs:
        start_h = s[0]
        # 高峰开始前 30 分钟 + 高峰结束后 30 分钟
        t1 = (start_h - 0.5) % 24
        t2 = (s[-1] + 1.5) % 24
        checks.append((t1, t2))
    for t1, t2 in checks:
        print(f"  {t1:5.1f}时 和 {t2:5.1f}时")
    return 0


if __name__ == "__main__":
    sys.exit(main())
