"""A/B 对照: 同一批帧上跑 Xiaomi 和 Aoyuon, 逐帧比较输出

关键点(上一版错了的地方):
  不能拿 data/subtitles/*.txt 当参照去按时间戳对齐 —— 那是去重后的行,
  时间戳是按 int(i*interval) 生成的, 用别的公式反推会对不上帧。
  这里两个引擎吃**同一帧、同一裁切带**, 逐帧比, 才是同口径。

输出:
  逐帧: 完全一致率 / CER / 两侧 p50·p95 延迟
  逐行: 各自去重后的行序列, 按行号对齐再比一次
"""
import argparse
import glob
import os
import re
import statistics
import sys
import time

import cv2

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

CLEAN_RE = re.compile(r"[\s\u3000·•\-\|/\\,，。.、;；:：!！?？\"'“”‘’()（）\[\]【】<>《》]+")


def norm(s):
    return CLEAN_RE.sub("", s or "")


def cer(ref, hyp):
    if not ref:
        return 0.0 if not hyp else 1.0
    # 纯 python 编辑距离, 避免额外依赖
    prev = list(range(len(hyp) + 1))
    for i, rc in enumerate(ref, 1):
        cur = [i]
        for j, hc in enumerate(hyp, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rc != hc)))
        prev = cur
    return prev[-1] / len(ref)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", default="data/frames")
    ap.add_argument("--bvid", default="BV1x3y8zpEGD")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    import monitor as M
    from xiaomi_ocr import XiaomiOCR
    from aoyuon.recognize import Recognizer

    frames = sorted(glob.glob(os.path.join(a.frames, a.bvid, "*.jpg")))
    if a.limit:
        frames = frames[:a.limit]
    if not frames:
        print("没有帧:", a.frames, a.bvid)
        return 1

    band = M.probe_band_heuristic(frames)
    print(f"帧数={len(frames)} 字幕带={band}", flush=True)

    cfg = M.load_config() if hasattr(M, "load_config") else {}
    xcfg = (cfg.get("xiaomi_ocr") or {}) if isinstance(cfg, dict) else {}
    xm = XiaomiOCR(xcfg, log=print)
    if not xm.start():
        print("Xiaomi 启动失败, 只能测 Aoyuon")
    rec = Recognizer(threads=4)

    tx, ta, rows = [], [], []
    for i, p in enumerate(frames):
        img = cv2.imread(p)
        if img is None:
            continue
        h = img.shape[0]
        crop = img[int(h * band[0]):int(h * band[1])] if band else img

        t0 = time.perf_counter()
        xb = xm(crop) if xm is not None and not xm.dead else []
        t1 = time.perf_counter()

        t2 = time.perf_counter()
        ab = rec(crop, is_band=True)
        t3 = time.perf_counter()

        xt = " ".join(b[2] for b in xb if b[2]).strip()
        at = " ".join(b[2] for b in ab if b[2]).strip()
        tx.append(t1 - t0)
        ta.append(t3 - t2)
        rows.append((i, xt, at))
        if (i + 1) % 20 == 0:
            print(f"  {i+1}/{len(frames)} xiaomi={t1-t0:.2f}s aoyuon={t3-t2:.3f}s", flush=True)

    if xm is not None:
        xm.stop()

    def pct(v, q):
        v = sorted(v)
        return v[min(len(v) - 1, int(len(v) * q))] * 1000

    print(f"\n延迟  Xiaomi p50={pct(tx,.5):.1f}ms p95={pct(tx,.95):.1f}ms"
          f"   Aoyuon p50={pct(ta,.5):.1f}ms p95={pct(ta,.95):.1f}ms")

    # 逐帧比较(只比两边都有字的帧, 空白帧单独计数)
    both = [(x, y) for _, x, y in rows if norm(x) and norm(y)]
    miss_x = sum(1 for _, x, y in rows if not norm(x) and norm(y))
    miss_a = sum(1 for _, x, y in rows if norm(x) and not norm(y))
    exact = sum(1 for x, y in both if norm(x) == norm(y))
    cers = [cer(norm(x), norm(y)) for x, y in both]
    n = max(1, len(both))
    print(f"有字帧={n}  xiaomi空/aoyuon有={miss_x}  xiaomi有/aoyuon空={miss_a}")
    print(f"逐帧完全一致={exact}/{n} = {100*exact/n:.2f}%   CER均值={100*statistics.mean(cers):.2f}%")

    # 逐行: 各自按"相邻相似度>0.85 去重"后比较行序列
    from difflib import SequenceMatcher

    def dedupe(idx_texts):
        out, last = [], ""
        for i, t in idx_texts:
            nn = norm(t)
            if not nn:
                continue
            if nn == last or (last and SequenceMatcher(None, nn, last).ratio() > 0.85):
                continue
            last = nn
            out.append((i, t))
        return out

    lx = dedupe([(i, x) for i, x, _ in rows])
    la = dedupe([(i, y) for i, _, y in rows])
    print(f"去重后行数  Xiaomi={len(lx)}  Aoyuon={len(la)}")

    diffs = [(x, y) for (i, x), (j, y) in zip(lx, la) if norm(x) != norm(y)]
    print(f"逐行完全一致={len(lx)-len(diffs)}/{len(lx)}")
    for x, y in diffs[:10]:
        print(f"  Xiaomi: {x}\n  Aoyuon: {y}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
