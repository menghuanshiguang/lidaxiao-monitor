"""Aoyuon-OCR 在我们素材上的实测: 速度 + 与现网 Xiaomi 字幕的逐行一致性

用法:
    python bench/aoyuon_bench.py --frames data/frames --subs data/subtitles \
        --bvid BV1x3y8zpEGD --mode frame|band

对照口径: 现网字幕 data/subtitles/<bvid>.txt 是 Xiaomi-OCR 逐帧识别+去重后的结果,
把它当作"上一版最好的答案", 统计新引擎和它的行级一致率与 CER。
"""
import argparse
import glob
import os
import re
import sys
import time
from difflib import SequenceMatcher

import cv2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aoyuon.recognize import Recognizer          # noqa: E402

TS_RE = re.compile(r"^\[(\d+):(\d+)\]\s*(.+)$")
CLEAN_RE = re.compile(r"[\s\u3000·•\-\|/\\,，。.、;；:：!！?？\"'“”‘’()（）\[\]【】<>《》]+")


def norm(s):
    return CLEAN_RE.sub("", s)


def load_subs(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            m = TS_RE.match(ln.strip())
            if m:
                out.append((int(m.group(1)) * 60 + int(m.group(2)), m.group(3).strip()))
    return out


def cer(ref, hyp):
    if not ref:
        return 0.0 if not hyp else 1.0
    return 1 - SequenceMatcher(None, ref, hyp).ratio()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", default="data/frames")
    ap.add_argument("--subs", default="data/subtitles")
    ap.add_argument("--bvid", default="BV1x3y8zpEGD")
    ap.add_argument("--mode", default="frame", choices=["frame", "band"])
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    frames = sorted(glob.glob(os.path.join(a.frames, a.bvid, "*.jpg")))
    if a.limit:
        frames = frames[:a.limit]
    subs = load_subs(os.path.join(a.subs, f"{a.bvid}.txt"))
    if not frames:
        print(f"没有帧: {a.frames}/{a.bvid}")
        return 1
    print(f"帧={len(frames)} 参考字幕行={len(subs)} 模式={a.mode}")

    rec = Recognizer(threads=4)
    from monitor import probe_band_heuristic          # 复用我们的带定位
    band = probe_band_heuristic(frames) if a.mode == "band" else None
    print(f"字幕带={band}")

    times, lines, last = [], [], ""
    t_all = time.time()
    for i, p in enumerate(frames):
        img = cv2.imread(p)
        if img is None:
            continue
        if band:
            h = img.shape[0]
            img = img[int(h * band[0]):int(h * band[1])]
        t0 = time.perf_counter()
        boxes = rec(img)
        times.append(time.perf_counter() - t0)
        text = " ".join(b[2] for b in boxes if b[2]).strip()
        n = norm(text)
        if not n or n == last:
            continue
        if last and SequenceMatcher(None, n, last).ratio() > 0.85:
            continue
        last = n
        lines.append((int(i * (subs[-1][0] / max(1, len(frames) - 1))) if subs else i, text))
    total = time.time() - t_all

    times.sort()
    p50 = times[len(times) // 2] * 1000
    p95 = times[int(len(times) * 0.95)] * 1000
    print(f"\n识别帧数={len(times)} 总耗时={total:.1f}s 每帧 p50={p50:.1f}ms p95={p95:.1f}ms")
    print(f"输出行={len(lines)} 参考行={len(subs)}")

    # 与参考字幕按时间戳对齐比较
    ref = {ts: txt for ts, txt in subs}
    hit = 0
    chars_ref = chars_ok = 0
    worst = []
    for ts, txt in lines:
        near = min(ref, key=lambda k: abs(k - ts)) if ref else None
        if near is None:
            continue
        r = ref[near]
        same = norm(txt) == norm(r)
        hit += same
        chars_ref += len(norm(r))
        chars_ok += len(norm(r)) - int(cer(norm(r), norm(txt)) * len(norm(r)))
        if not same:
            worst.append((cer(norm(r), norm(txt)), r, txt))
    n = max(1, len(lines))
    print(f"行级完全命中: {hit}/{n} = {100*hit/n:.2f}%")
    print(f"参考侧字数={chars_ref} 正确≈{chars_ok} CER≈{100*(1-chars_ok/max(1,chars_ref)):.2f}%")
    worst.sort(reverse=True)
    print("\n差异最大的 10 行:")
    for c, r, g in worst[:10]:
        print(f"  CER={c:.2f}\n    Xiaomi: {r}\n    Aoyuon: {g}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
