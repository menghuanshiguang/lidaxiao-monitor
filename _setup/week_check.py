#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import datetime, json, os

data = json.load(open(r"C:\Users\fantasytat\Desktop\dsh\daxiao\data\pubdates.json", encoding="utf-8"))
tz = datetime.timezone(datetime.timedelta(hours=8))
cutoff = datetime.datetime(2026, 8, 7, tzinfo=tz).timestamp()
week = [v for v in data if v["created"] >= cutoff]
week.sort(key=lambda v: v["created"])
print(f"最近一周(8-7~8-14)视频: {len(week)} 条")
for v in week:
    dt = datetime.datetime.fromtimestamp(v["created"], tz)
    has = os.path.exists(rf"C:\Users\fantasytat\Desktop\dsh\daxiao\data\subtitles\{v['bvid']}.txt")
    size = 0
    if has:
        size = os.path.getsize(rf"C:\Users\fantasytat\Desktop\dsh\daxiao\data\subtitles\{v['bvid']}.txt")
    print(f"  {dt:%m-%d %H:%M} {v['bvid']} [{v['length']}] {v['title'][:45]} 字幕:{'✓' if size>0 else ('空' if has else '✗')}({size}B)")
