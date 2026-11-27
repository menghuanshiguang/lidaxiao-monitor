"""用 Aoyuon 权重识别一张任意截图（不是视频帧）

两种口径都跑，看模型在不同输入下的表现：
  A. 整图直喂 —— 走模型自带的字幕行检测（训练分布是白字黑边的视频帧）
  B. 自行切行 —— 截图是黑字白底，先反相再做行投影，逐行裁切后按单行识别

B 才是"识别能力"的真实检验；A 用来观察检测器在域外输入上的行为。
"""
import argparse
import sys

import cv2
import numpy as np

sys.path.insert(0, __file__.rsplit("/", 2)[0])
from aoyuon.recognize import Recognizer          # noqa: E402


def rows_dark_on_light(gray, thr=160, min_h=8, gap=4):
    """黑字白底：反相后做行投影，返回 [(y0,y1)]"""
    inv = 255 - gray
    ink = (inv > 255 - thr).astype(np.uint8)
    prof = ink.sum(axis=1)
    if prof.max() < 10:
        return []
    t = max(5, prof.max() * 0.06)
    runs, start, miss = [], None, 0
    for y, v in enumerate(prof):
        if v > t:
            if start is None:
                start = y
            miss = 0
        elif start is not None:
            miss += 1
            if miss > gap:
                runs.append((start, y - miss))
                start = None
    if start is not None:
        runs.append((start, len(prof) - 1))
    return [(a, b) for a, b in runs if b - a >= min_h]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default="bench/images/comment.png")
    ap.add_argument("--limit", type=int, default=14)
    a = ap.parse_args()

    img = cv2.imread(a.image)
    if img is None:
        print("读不到图:", a.image)
        return 1
    h, w = img.shape[:2]
    print(f"图像 {w}x{h}")
    rec = Recognizer(threads=4)

    print("\n=== A. 整图直喂（走它的字幕行检测）===")
    boxes = rec(img)
    print(f"检出 {len(boxes)} 行")
    for y, x, text, score in boxes[:8]:
        print(f"  y={y:4.0f} score={score:.2f} {text}")

    print("\n=== B. 自行切行（反相 + 行投影，逐行识别）===")
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    rows = rows_dark_on_light(gray)
    # 模型训练分布是"白字黑底", 截图是黑字白底 -> 反相后再裁, 否则等于喂反色
    inv_img = cv2.bitwise_not(img)
    print(f"切出 {len(rows)} 行，取前 {a.limit} 行识别：")
    ok = 0
    for y0, y1 in rows[: a.limit]:
        pad = 6
        crop = inv_img[max(0, y0 - pad):min(h, y1 + pad), :]
        out = rec(crop, is_band=True)
        text = " ".join(o[2] for o in out if o[2]).strip()
        score = min((o[3] for o in out), default=0.0)
        if text:
            ok += 1
        print(f"  [{y0:4d}-{y1:4d}] score={score:.2f} {text}")
    print(f"\n有输出行数: {ok}/{min(len(rows), a.limit)}")

    print("\n=== C. 整图反相后直喂（极性修正后的检测）===")
    boxes2 = rec(inv_img)
    print(f"检出 {len(boxes2)} 行")
    for y, x, text, score in boxes2[:8]:
        print(f"  y={y:4.0f} score={score:.2f} {text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
