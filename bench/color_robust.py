"""换字幕颜色后的鲁棒性测试

两层分开看:
  1. 检测: find_line_boxes 是否仍能找到行 (阈值是亮度的, 换深色可能直接失效)
  2. 识别: 同一行在灰度化之后的亮度差变了, 输出会差多少

对比基准: 原图(白字)的识别结果。
"""
import argparse
import glob
import sys

import cv2
import numpy as np

sys.path.insert(0, __file__.rsplit("/", 2)[0])
from aoyuon.band import find_line_boxes          # noqa: E402
from aoyuon.recognize import Recognizer          # noqa: E402

COLORS = {
    "白": (255, 255, 255),
    "黄": (0, 255, 255),
    "青": (255, 255, 0),
    "绿": (0, 255, 0),
    "红": (0, 0, 255),
    "蓝": (255, 0, 0),
}


def edit_dist(a, b):
    if not a:
        return len(b)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def cer(ref, hyp):
    return edit_dist(ref, hyp) / max(1, len(ref))


def recolor(img, bgr):
    out = img.copy()
    m = (out[:, :, 0] >= 230) & (out[:, :, 1] >= 230) & (out[:, :, 2] >= 230)
    out[m] = bgr
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", default="bench/frames/BV1x3y8zpEGD")
    ap.add_argument("--limit", type=int, default=24)
    a = ap.parse_args()

    frames = sorted(glob.glob(a.frames + "/*.jpg"))[: a.limit]
    rec = Recognizer(threads=4)

    def run(img):
        boxes = find_line_boxes(img)
        if not boxes:
            return "", 0
        txt = " ".join(b[2] for b in rec(img) if b[2])
        return txt.strip(), len(boxes)

    base = [run(cv2.imread(p)) for p in frames]
    base_txt = [t for t, _ in base]
    print(f"帧数={len(frames)} 基准行数(有字)={sum(1 for t in base_txt if t)}", flush=True)

    print(f"\n{'颜色':<4} {'灰度':>4} {'检出行帧数':>10} {'有字帧':>6} {'与白色基准CER':>14} {'完全一致':>8}")
    for name, bgr in COLORS.items():
        gray = int(cv2.cvtColor(np.uint8([[[bgr[0], bgr[1], bgr[2]]]]),
                                cv2.COLOR_BGR2GRAY)[0, 0])
        got, detect_ok = [], 0
        for p in frames:
            img = recolor(cv2.imread(p), bgr)
            txt, nb = run(img)
            got.append(txt)
            if nb > 0:
                detect_ok += 1
        pairs = [(b, g) for b, g in zip(base_txt, got) if b]
        c = float(np.mean([cer(b, g) for b, g in pairs])) if pairs else 0.0
        same = sum(1 for b, g in pairs if b == g)
        print(f"{name:<4} {gray:>4} {detect_ok:>10} {sum(1 for g in got if g):>6} "
              f"{100*c:>13.2f}% {same:>4}/{len(pairs)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
