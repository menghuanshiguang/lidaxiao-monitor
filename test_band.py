"""字幕带探测单测: 行投影要能定位白字黑边, 且不吃水印/不框半屏"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from PIL import Image, ImageDraw
import monitor

ok = fail = 0


def chk(name, cond, info=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✅ {name}")
    else:
        fail += 1
        print(f"  ❌ {name}\n     {info}")


tmp = tempfile.mkdtemp()


def make(paths, top_pct, bot_pct, watermark=False, n=8):
    W, H = 640, 360
    out = []
    for i in range(n):
        im = Image.new("L", (W, H), 110)
        d = ImageDraw.Draw(im)
        y0, y1 = int(H * top_pct), int(H * bot_pct)
        # 模拟汉字笔画: 4px 粗的白条 + 1px 黑描边 (实心大块不符合真实字幕)
        y = y0
        while y < y1:
            d.rectangle([61, y + 1, 579, y + 4], outline=0, fill=255)
            y += 9
        if watermark:      # 右上角浅色水印, 不该被当成字幕
            d.rectangle([560, 40, 630, 60], fill=180)
        p = os.path.join(tmp, f"w{i}.jpg")
        im.save(p)
        out.append(p)
    return out


b = monitor.probe_band_heuristic(make([], 0.86, 0.92))
chk("底部单行能定位", b and b[0] <= 0.86 and b[1] >= 0.92, b)
chk("不框进半屏", b and (b[1] - b[0]) < 0.25, b)

b2 = monitor.probe_band_heuristic(make([], 0.84, 0.95))
chk("两行范围包住", b2 and b2[0] <= 0.84 and b2[1] >= 0.95, b2)

b3 = monitor.probe_band_heuristic(make([], 0.86, 0.92, watermark=True))
chk("水印不干扰", b3 and b3[0] > 0.7, b3)

# 纯色帧 -> 返回 None (调用方回退默认带)
blank = []
for i in range(8):
    p = os.path.join(tmp, f"b{i}.jpg")
    Image.new("L", (640, 360), 120).save(p)
    blank.append(p)
chk("无文字返回 None", monitor.probe_band_heuristic(blank) is None)

print(f"\n{ok} passed, {fail} failed")
raise SystemExit(1 if fail else 0)
