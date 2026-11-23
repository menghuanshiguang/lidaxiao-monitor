# -*- coding: utf-8 -*-
"""字幕带检测 (白字 + 深色描边) —— 训练裁图和推理裁图共用同一实现。

设计取舍:
  * 只统计「白像素且其水平邻域内存在深色像素」的像素, 这样画面里的白墙、
    高光、白衬衫不会单独把行投影顶起来, 真正的描边字才会被计入。
  * 行分数按峰值的比例阈值取行, 收集连续段, 允许两行字幕 (段间 <= gap 合并)。
  * 只在画面下部分 bottom_frac 内搜索: 这些视频的字幕永远在下方。
"""
from __future__ import annotations

import numpy as np

WHITE = 215      # 白字灰度下限
DARK = 95        # 描边灰度上限
DARK_RADIUS = 9  # 水平邻域半径 (像素)
ROW_THR_RATIO = 0.10
MIN_PEAK = 10
MERGE_GAP = 5


def _gray(img):
    if img.ndim == 3:
        b, g, r = img[..., 0], img[..., 1], img[..., 2]
        return (b.astype(np.int32) * 114 + g.astype(np.int32) * 587 +
                r.astype(np.int32) * 299) // 1000
    return img.astype(np.int32)


def row_scores(img_bgr, white=WHITE, dark=DARK, radius=DARK_RADIUS):
    """返回每行「描边白字」像素数 (float32, 长度 = h)。"""
    g = _gray(img_bgr)
    h, w = g.shape
    white_m = (g >= white).astype(np.float32)
    dark_m = (g <= dark)
    # 水平膨胀: 每个像素取 [x-radius, x+radius] 区间内是否存在深色像素
    k = 2 * radius + 1
    if w >= k:
        cs = np.zeros((h, w + 1), np.int32)
        cs[:, 1:] = np.cumsum(dark_m, axis=1)
        left = np.clip(np.arange(w) - radius, 0, w)
        right = np.clip(np.arange(w) + radius + 1, 0, w)
        dark_near = (cs[:, right] - cs[:, left]) > 0
    else:
        dark_near = np.repeat(dark_m.any(axis=1, keepdims=True), w, axis=1)
    return (white_m * dark_near).sum(axis=1).astype(np.float32)


def find_text_band(img_bgr, bottom_frac=0.50, min_h=14, max_h_frac=0.22,
                   row_thr_ratio=ROW_THR_RATIO, min_peak=MIN_PEAK, pad_frac=0.15):
    """返回 (y0, y1, score) 或 None。score 是判定用的行分数峰值。"""
    h = img_bgr.shape[0]
    rs = row_scores(img_bgr)
    y_start = int(h * (1.0 - bottom_frac))
    seg = rs[y_start:]
    if seg.size == 0 or seg.max() < min_peak:
        return None
    peak = float(seg.max())
    thr = max(4.0, peak * row_thr_ratio)
    rows = np.flatnonzero(seg >= thr)
    if rows.size == 0:
        return None
    runs = np.split(rows, np.flatnonzero(np.diff(rows) > MERGE_GAP) + 1)
    # 候选段: 位于画面最下方 max_h_frac 高度内的段
    y_low_limit = int(h * (1.0 - max_h_frac))
    cands = [r for r in runs if (y_start + r[-1]) >= y_low_limit]
    if not cands:
        cands = runs
    # 取分数总和最高的候选段
    best, best_score = None, -1.0
    for r in cands:
        s = float(rs[y_start + r[0]: y_start + r[-1] + 1].sum())
        if s > best_score:
            best, best_score = r, s
    y0 = y_start + int(best[0])
    y1 = y_start + int(best[-1]) + 1
    pad = max(2, int((y1 - y0) * pad_frac))
    y0 = max(0, y0 - pad)
    y1 = min(h, y1 + pad)
    if y1 - y0 < min_h:
        return None
    return int(y0), int(y1), peak


def crop_band(img_bgr, band):
    return img_bgr[band[0]:band[1], :]


def find_text_box(img_bgr, pad_x=6, pad_y=None, band_override=None, **kw):
    """在行带基础上再收紧左右边界, 返回 (y0, y1, x0, x1) 或 None。

    老师模型和训练裁图都用它: 图像 token 数直接决定 llama-server 的耗时,
    把左右空白切掉能让老师的单帧耗时降下来, 也让小模型看到的是紧边界。
    band_override=(y0, y1, peak) 时跳过行带检测, 直接用给的行范围。
    """
    band = band_override if band_override is not None else find_text_band(img_bgr, **kw)
    if band is None:
        return None
    y0, y1, _ = band
    g = _gray(img_bgr)
    sub_w = (g[y0:y1] >= WHITE)
    sub_d = (g[y0:y1] <= DARK)
    hgt, wid = sub_w.shape
    if hgt < 2 or wid < 2:
        return None
    # 垂直膨胀: 列的上下邻域内是否存在描边
    k = min(5, hgt)
    cs = np.zeros((hgt + 1, wid), np.int32)
    cs[1:] = np.cumsum(sub_d, axis=0)
    top = np.clip(np.arange(hgt) - 2, 0, hgt)
    bot = np.clip(np.arange(hgt) + 3, 0, hgt)
    dark_near = (cs[bot] - cs[top]) > 0          # (hgt, wid)
    col = (sub_w & dark_near).sum(axis=0).astype(np.float32)
    if col.max() < 2:
        return None
    cols = np.flatnonzero(col >= max(2.0, col.max() * 0.06))
    if cols.size == 0:
        return None
    x0 = max(0, int(cols[0]) - pad_x)
    x1 = min(wid, int(cols[-1]) + 1 + pad_x)
    py = pad_y if pad_y is not None else max(2, int((y1 - y0) * 0.10))
    y0 = max(0, y0 - py)
    y1 = min(img_bgr.shape[0], y1 + py)
    if x1 - x0 < 16 or y1 - y0 < 12:
        return None
    return int(y0), int(y1), int(x0), int(x1)


def _x_trim(g, y0, y1, x0, x1, pad_x):
    """在 [y0:y1, x0:x1] 内按"白字带描边"的列支撑收紧左右边界。"""
    sw = (g[y0:y1, x0:x1] >= WHITE)
    sd = (g[y0:y1, x0:x1] <= DARK)
    hh, ww = sw.shape
    if hh < 4 or ww < 8:
        return None
    cs = np.zeros((hh + 1, ww), np.int32)
    cs[1:] = np.cumsum(sd, axis=0)
    t2 = np.clip(np.arange(hh) - 2, 0, hh)
    b2 = np.clip(np.arange(hh) + 3, 0, hh)
    col = (sw & ((cs[b2] - cs[t2]) > 0)).sum(axis=0).astype(np.float32)
    if col.max() < 2:
        return None
    cols = np.flatnonzero(col >= max(2.0, col.max() * 0.06))
    if cols.size == 0:
        return None
    lx0 = max(0, x0 + int(cols[0]) - pad_x)
    lx1 = min(g.shape[1], x0 + int(cols[-1]) + 1 + pad_x)
    if lx1 - lx0 < 16:
        return None
    return lx0, lx1


def _row_runs(prof, thr, min_len=8, gap=2):
    rows = np.flatnonzero(prof >= thr)
    if rows.size == 0:
        return []
    runs = np.split(rows, np.flatnonzero(np.diff(rows) > gap) + 1)
    return [(int(r[0]), int(r[-1]) + 1) for r in runs if len(r) >= min_len]


def _dilate1d(prof, r=2):
    """1-D 最大值滤波: 填掉行投影里的浅谷(短行内部笔画间隙), 避免把一行切碎。"""
    if r <= 0 or prof.size == 0:
        return prof
    pad = np.pad(prof, r, mode="edge")
    k = 2 * r + 1
    return np.max(np.stack([pad[i:i + prof.size] for i in range(k)]), axis=0)


def _line_in_span(rs, lo, hi, min_peak_ratio=None, ref_peak=None):
    """在绝对行号 [lo, hi) 里找一段像文字行的高支撑行段, 返回 (a, b) 或 None。

    与主搜索的区别: 阈值按**本段自己的** p99 取 0.25, 并先做 1-D 膨胀。
    这样 2~4 字的短行(支撑只有主行的 1/4)也能留住 —— 见 issue #1。
    """
    lo = max(0, int(lo))
    hi = min(int(hi), rs.size)
    if hi - lo < 6:
        return None
    seg = _dilate1d(rs[lo:hi], 2)
    p = float(np.percentile(seg, 99))
    if p < 8:
        return None
    if min_peak_ratio is not None and ref_peak and p < min_peak_ratio * ref_peak:
        return None
    thr = max(3.0, 0.25 * p)
    runs = _row_runs(seg, thr, min_len=6, gap=2)
    if not runs:
        return None
    a, b = max(runs, key=lambda r: float(seg[r[0]:r[1]].sum()))
    return (lo + a, lo + b)


def _like_line(span, h_ref, lo=0.55, hi=1.6):
    """补找出来的行要和已确认行"长得像一行": 高度在同一量级。"""
    h = span[1] - span[0]
    return 6 <= h <= hi * h_ref and h >= lo * h_ref


def find_line_boxes(img_bgr, box=None, pad_x=6, pad_y=3, max_lines=4,
                    bottom_frac=0.55, y_min_frac=0.68, neighbor=True, **kw):
    """返回字幕的逐行框 [(y0, y1, x0, x1), ...], 每行一个, 绝对坐标。

    box 给定时只在该区域内切行; 否则自己在画面下方找字幕行:
    行投影(白字+水平邻域有描边) -> 取画面下方且高度像一行的连续段 -> 按间隙分簇
    -> 取总支撑最强的一簇。阈值用 0.12 * peak, 两行字幕里短的那行也留得住。
    """
    h_img, w_img = img_bgr.shape[:2]
    g = _gray(img_bgr)
    rs = row_scores(img_bgr)
    if box is None:
        y_start = int(h_img * (1.0 - bottom_frac))
        seg = rs[y_start:]
        peak = float(seg.max()) if seg.size else 0.0
        if peak < MIN_PEAK:
            return []
        thr = max(4.0, 0.12 * peak)
        runs = _row_runs(seg, thr, min_len=8, gap=2)
        runs = [(a + y_start, b + y_start) for a, b in runs]
        h_med = float(np.median([b - a for a, b in runs])) if runs else 0.0
        keep = []
        for a, b in runs:
            hh = b - a
            if hh > 0.16 * h_img or hh < 8:
                continue
            if b < int(h_img * y_min_frac):
                continue
            if float(rs[a:b].max()) < 0.35 * peak:
                continue
            keep.append((a, b))
        if not keep:
            return []
        gap = max(3, int(0.45 * h_med)) if h_med else 4
        clusters = [[keep[0]]]
        for a, b in keep[1:]:
            if a - clusters[-1][-1][1] <= gap:
                clusters[-1].append((a, b))
            else:
                clusters.append([(a, b)])
        best = max(clusters, key=lambda c: float(sum(rs[a:b].sum() for a, b in c)))
        lines_rows = [(a, b) for a, b in best]
        x0, x1 = 0, w_img
    else:
        y0, y1, x0, x1 = [int(v) for v in box]
        y0, y1 = max(0, y0), min(h_img, y1)
        x0, x1 = max(0, x0), min(w_img, x1)
        prof = rs[y0:y1]
        if prof.size == 0:
            return []
        hi = float(np.percentile(prof, 99))
        if hi < 8:
            return []
        thr = max(3.0, 0.10 * hi)
        runs = _row_runs(prof, thr, min_len=8, gap=2)
        if not runs:
            return []
        hmax = max(b - a for a, b in runs)
        runs = [(a, b) for a, b in runs if (b - a) >= 0.45 * hmax]
        lines_rows = [(y0 + a, y0 + b) for a, b in runs]

    # 邻行兜底: 折行的短行(2~4 字, 行投影只有主行的 1/4)会被上面的阈值吃掉,
    # 在已确认行的上下各再找一段, 距离放 1.5 倍行高以内(字幕折行总是紧贴的)。
    lines_rows = sorted(set(lines_rows))
    lo_reg, hi_reg = (y0, y1) if box is not None else (y_start, h_img)
    ref = float(rs[lines_rows[0][0]:lines_rows[-1][1]].max()) if lines_rows else 0.0
    while neighbor and lines_rows and len(lines_rows) < max_lines:
        h_cur = max(b - a for a, b in lines_rows)
        added = False
        cur = lines_rows[-1][1]
        if cur < hi_reg - 4:
            got = _line_in_span(rs, cur, hi_reg, 0.15, ref)
            if got and _like_line(got, h_cur) and got[0] - cur <= 1.5 * h_cur:
                lines_rows.append(got)
                added = True
        if len(lines_rows) < max_lines:
            cur = lines_rows[0][0]
            if cur > lo_reg + 4:
                got = _line_in_span(rs, lo_reg, cur, 0.15, ref)
                if got and _like_line(got, h_cur) and cur - got[1] <= 1.5 * h_cur:
                    lines_rows.insert(0, got)
                    added = True
        if not added:
            break

    out = []
    raw = []
    for a, b in lines_rows[:max_lines]:
        ny0 = max(0, a - pad_y)
        ny1 = min(h_img, b + pad_y)
        xr = _x_trim(g, a, b, x0, x1, pad_x)
        if xr is None:
            continue
        out.append((int(ny0), int(ny1), int(xr[0]), int(xr[1])))
        raw.append((int(a), int(b)))
    # 相邻两行的 pad 不要互相吃进来: 在原始间隙中点处切断
    for i in range(len(out) - 1):
        if out[i][1] > out[i + 1][0]:
            mid = (raw[i][1] + raw[i + 1][0]) // 2
            out[i] = (out[i][0], min(out[i][1], mid), out[i][2], out[i][3])
            out[i + 1] = (max(out[i + 1][0], mid), out[i + 1][1], out[i + 1][2], out[i + 1][3])
    return out
