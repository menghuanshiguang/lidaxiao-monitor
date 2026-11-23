"""图像通道基础件: 抽帧 / 单帧 OCR / 字幕带探测 (从 monitor.py 抽出)"""
import glob
import os
import subprocess

from lidaxiao.common import FFMPEG, FFPROBE, WORKDIR
from lidaxiao.textutil import log


def extract_frames(video_path, bvid, interval):
    """ffmpeg 按 interval 秒抽帧到 data/frames/<bvid>/"""
    fdir = os.path.join(WORKDIR, "data", "frames", bvid)
    if os.path.isdir(fdir) and glob.glob(os.path.join(fdir, "*.jpg")):
        return sorted(glob.glob(os.path.join(fdir, "*.jpg")))
    os.makedirs(fdir, exist_ok=True)
    pat = os.path.join(fdir, "f_%06d.jpg")
    cmd = [FFMPEG, "-y", "-v", "error", "-i", video_path,
           "-vf", f"fps=1/{interval:.4f}", "-q:v", "4", pat]
    log(f"抽帧: interval={interval}s -> {fdir}")
    try:
        subprocess.run(cmd, check=True, timeout=3600,
                       capture_output=True, text=True)
    except Exception as e:
        log(f"ffmpeg 抽帧失败: {e}")
        return []
    return sorted(glob.glob(os.path.join(fdir, "*.jpg")))

def ocr_frame(engine, img_path, band=None):
    """band=(y0,y1) 0~1 相对高度, None=整帧"""
    import cv2
    img = cv2.imread(img_path)
    if img is None:
        return []
    h, w = img.shape[:2]
    if band:
        y0, y1 = int(h * band[0]), int(h * band[1])
        img = img[max(0, y0):max(0, y1), :]
    if getattr(engine, "provides_boxes", False):
        return engine(img)          # Xiaomi-OCR: 直接返回 (y,x,text,score)
    result, _ = engine(img)
    boxes = []
    if result:
        for item in result:
            try:
                box, text, score = item[0], item[1], item[2]
            except Exception:
                continue
            ys = [p[1] for p in box]
            boxes.append((min(ys), min(p[0] for p in box), text, float(score)))
    boxes.sort(key=lambda t: (t[0] // 20, t[1]))
    return boxes

def probe_subtitle_band(engine, frames, conf):
    """探测字幕带 (y0,y1): 排除顶部logo/右上角水印, 优先底部字幕带"""
    import cv2
    img = cv2.imread(frames[0])
    if img is None:
        return None
    h, w = img.shape[:2]
    ys = []
    for f in frames[:min(12, len(frames))]:
        for y, x, text, score in ocr_frame(engine, f):
            if score >= conf:
                # 排除右上角日期水印(x>0.82w)与顶部 logo(y<0.10h)
                if x < 0.82 * w and y > 0.10 * h:
                    ys.append(y)
    if not ys:
        return None
    bottom = [y for y in ys if y > 0.60 * h]
    if len(bottom) >= max(1, len(ys) // 4):
        band = (0.70, 0.99)
        log(f"字幕带探测: 底部字幕带 ({band[0]:.2f},{band[1]:.2f})")
        return band
    med = sorted(ys)[len(ys) // 2] / h
    y0 = max(0.35, min(med - 0.13, 0.80))
    y1 = min(1.0, y0 + 0.30)
    log(f"字幕带探测: 中位y={med:.2f}, 带=({y0:.2f},{y1:.2f})")
    return (y0, y1)

def probe_band_heuristic(frames, samples=8, min_h=0.18):
    """用行投影找字幕位置 (不依赖模型坐标)

    Xiaomi-OCR 只回文本, y 是按行号等分的伪坐标, 没法用来定位 -> 改从画面本身找:
    字幕是"纯白字 + 黑描边", 先取 [>=225 的白] ∩ [<=85 的黑膨胀], 再按行投影。
    主窗口取下半屏得分最高的 7% 高度区块, 再向两侧吞并得分 >15% 峰值的相邻行,
    这样两行字幕能整段包住, 而单行字幕不会把半屏都框进来。
    """
    from PIL import Image, ImageFilter, ImageChops
    n = len(frames)
    if not n:
        return None
    idx = sorted({int(n * k / samples) for k in range(samples)})
    tot, h, w = None, 0, 0
    for i in idx:
        try:
            with Image.open(frames[i]) as im:
                g = im.convert("L")
                w, h = g.size
                white = g.point(lambda v: 255 if v >= 225 else 0)
                dark = g.point(lambda v: 255 if v <= 85 else 0).filter(ImageFilter.MaxFilter(5))
                t = ImageChops.multiply(white, dark)
                d = list(t.getdata())
        except Exception as e:
            log(f"⚠ 行投影取样失败(帧{i}): {e}")
            continue
        if tot is None:
            tot = [0] * h
        for y in range(h):
            row = d[y * w:(y + 1) * w]
            tot[y] += sum(1 for v in row if v)
    if not tot or max(tot) < 20:
        return None
    win = max(4, int(0.07 * h))
    peak, start = max(((sum(tot[y:y + win]), y) for y in range(int(0.45 * h), max(1, h - win))))
    thr = peak * 0.15
    # 允许跨过行间空白(两行字幕之间、字与字的上下留白), 否则只会框住一行
    gap = max(2, int(0.035 * h))
    lo, hi = start, start + win
    miss, y = 0, start - 1
    while y >= 0:
        if tot[y] > thr:
            lo, miss = y, 0
        else:
            miss += 1
            if miss > gap:
                break
        y -= 1
    miss, y = 0, hi
    while y < h:
        if tot[y] > thr:
            hi, miss = y + 1, 0
        else:
            miss += 1
            if miss > gap:
                break
        y += 1
    # 片尾声明卡字号小、行数多, 比口播字幕更高, 边距要留够
    y0 = max(0.0, lo / h - 0.050)
    y1 = min(1.0, hi / h + 0.050)
    if y1 - y0 < min_h:
        c = (y0 + y1) / 2
        y0, y1 = max(0.0, c - min_h / 2), min(1.0, c + min_h / 2)
        y0 = max(0.0, y1 - min_h)
    band = (round(y0, 3), round(y1, 3))
    log(f"字幕带探测: 行投影 {len(idx)} 帧 -> ({band[0]:.2f},{band[1]:.2f})")
    return band


def ffprobe_duration(video_path):
    try:
        r = subprocess.run([FFPROBE, "-v", "error", "-show_entries",
                            "format=duration", "-of", "csv=p=0", video_path],
                           capture_output=True, text=True, timeout=60)
        return float(r.stdout.strip())
    except Exception:
        return 0.0
