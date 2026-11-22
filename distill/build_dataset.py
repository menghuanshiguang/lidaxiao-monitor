"""蒸馏数据集构建: 从抽帧 + 字幕文本 生成"单行字幕图 -> 文本"配对

标签对齐方式:
  data/subtitles/<bvid>.txt 是逐条 [mm:ss] 文本, 时间戳就是抽帧时刻(i * interval)。
  对每条字幕, 取它首次出现的帧到下一句之间的中间帧, 用该帧的**字幕带裁切**作为样本。

为什么取中间帧: 句首/句尾常有淡入和半透明, 字还没打全(实测"…永远比股票 重要"就丢过字)。

输出:
  out/images/<bvid>_<ts>.png   裁切后的字幕条
  out/manifest.tsv             文件名 \t 文本
"""
import argparse
import glob
import os
import re
import sys

from PIL import Image

TS_RE = re.compile(r"^\[(\d+):(\d+)\]\s*(.+)$")


def load_lines(sub_path):
    out = []
    with open(sub_path, encoding="utf-8") as f:
        for ln in f:
            m = TS_RE.match(ln.strip())
            if m:
                out.append((int(m.group(1)) * 60 + int(m.group(2)), m.group(3).strip()))
    return out


def frame_band_bbox(path, top=0.70, bright=225, dark=85):
    """用行投影找这一帧里文字的实际上下边界 (像素), 失败返回 None"""
    from PIL import ImageFilter, ImageChops
    with Image.open(path) as im:
        g = im.convert("L")
        w, h = g.size
        white = g.point(lambda v: 255 if v >= bright else 0)
        black = g.point(lambda v: 255 if v <= dark else 0).filter(ImageFilter.MaxFilter(5))
        mask = ImageChops.multiply(white, black)
        d = list(mask.getdata())
    rows = [sum(1 for v in d[y * w:(y + 1) * w] if v) for y in range(h)]
    y0 = int(h * top)
    peak = max(rows[y0:]) if rows[y0:] else 0
    if peak < 10:
        return None
    thr = peak * 0.15
    lo = hi = None
    for y in range(y0, h):
        if rows[y] > thr:
            lo = y if lo is None else lo
            hi = y
    if lo is None:
        return None
    pad = max(4, int(0.012 * h))
    return (0, max(0, lo - pad), w, min(h, hi + pad + 1))


def build(frames_root, subs_root, out_dir, pad_ratio=0.04):
    os.makedirs(os.path.join(out_dir, "images"), exist_ok=True)
    manifest = []
    stats = {"videos": 0, "samples": 0, "skip_no_band": 0, "skip_no_frame": 0}
    for bvid in sorted(os.listdir(frames_root)):
        fdir = os.path.join(frames_root, bvid)
        sub = os.path.join(subs_root, f"{bvid}.txt")
        if not os.path.isdir(fdir) or not os.path.exists(sub):
            continue
        frames = sorted(glob.glob(os.path.join(fdir, "*.jpg")))
        lines = load_lines(sub)
        if not frames or not lines:
            continue
        stats["videos"] += 1
        interval = 2.18                       # 生产默认; 有 ts 时以 ts 为准
        if len(lines) > 1:
            interval = (lines[-1][0] - lines[0][0]) / max(1, len(lines) - 1)
        for k, (ts, text) in enumerate(lines):
            nxt = lines[k + 1][0] if k + 1 < len(lines) else ts + 3
            i0, i1 = int(ts / interval), int(nxt / interval) - 1
            if i1 < i0:
                i1 = i0
            pick = min(len(frames) - 1, max(0, (i0 + i1) // 2))
            if pick >= len(frames):
                stats["skip_no_frame"] += 1
                continue
            bbox = frame_band_bbox(frames[pick])
            if not bbox:
                stats["skip_no_band"] += 1
                continue
            try:
                with Image.open(frames[pick]) as im:
                    crop = im.crop(bbox)
                    pad = int(crop.height * pad_ratio)
                    canvas = Image.new("RGB", (crop.width, crop.height + 2 * pad), (0, 0, 0))
                    canvas.paste(crop.convert("RGB"), (0, pad))
            except Exception:
                continue
            name = f"{bvid}_{ts:05d}.png"
            canvas.save(os.path.join(out_dir, "images", name))
            manifest.append((name, text))
            stats["samples"] += 1
    with open(os.path.join(out_dir, "manifest.tsv"), "w", encoding="utf-8") as f:
        for name, text in manifest:
            f.write(f"{name}\t{text}\n")
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", default="data/frames")
    ap.add_argument("--subs", default="data/subtitles")
    ap.add_argument("--out", default="distill/data")
    a = ap.parse_args()
    st = build(a.frames, a.subs, a.out)
    print(f"视频={st['videos']} 样本={st['samples']} "
          f"无字幕带={st['skip_no_band']} 越界={st['skip_no_frame']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
