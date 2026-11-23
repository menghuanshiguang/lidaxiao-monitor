# -*- coding: utf-8 -*-
"""训练 / 评估 / 推理共用的文本与图像预处理。

这里放"唯一实现"的东西: 字符表读写、CTC 贪心解码、编辑距离/CER、
裁好的单行图 -> 模型输入张量。train.py / recognize.py / eval.py 都从这里 import,
避免三份各写一遍慢慢漂移。
"""
from __future__ import annotations

import json
import os

import numpy as np

IMG_H = 48
MAX_W = 640
BLANK = 0


def read_charset(path):
    with open(path, encoding="utf-8") as f:
        return [ln.rstrip("\n") for ln in f]


def write_charset(path, chars):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for c in chars:
            f.write(c + "\n")


def read_config(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def preprocess(crop_bgr, height=IMG_H, max_width=MAX_W, to_float=True):
    """BGR 单行图 -> (1, height, W) float32 in [-1,1]; W 为 8 的倍数且 <= max_width。"""
    import cv2
    g = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY) if crop_bgr.ndim == 3 else crop_bgr
    h, w = g.shape[:2]
    if h <= 0 or w <= 0:
        raise ValueError("empty crop")
    nw = max(8, int(round(w * height / float(h))))
    if nw > max_width:
        nw = max_width
    g = cv2.resize(g, (nw, height), interpolation=cv2.INTER_LINEAR)
    pad = (-nw) % 8
    if pad:
        g = cv2.copyMakeBorder(g, 0, 0, 0, pad, cv2.BORDER_REPLICATE)
    if to_float:
        arr = g.astype(np.float32) / 255.0
        arr = (arr - 0.5) / 0.5
        return arr[None, :, :]
    return g


def ctc_greedy(logits, charset, blank=BLANK):
    """logits: (T, C) 或 (C, T) 的 log-probs 或 probs; 返回 (text, score)。"""
    a = np.asarray(logits)
    if a.ndim != 2:
        raise ValueError(f"expect 2-D, got {a.shape}")
    n_cls = len(charset) + 1
    if a.shape[1] == n_cls:
        pass
    elif a.shape[0] == n_cls:
        a = a.T
    else:
        raise ValueError(f"shape {a.shape} 与字符表 {len(charset)} 不匹配")
    # 归一到概率
    if a.min() < 0 or a.max() > 1.0001:
        a = a - a.max(axis=1, keepdims=True)
        e = np.exp(a)
        a = e / e.sum(axis=1, keepdims=True)
    idx = a.argmax(axis=1)
    scores = a[np.arange(len(idx)), idx]
    out, out_scores = [], []
    prev = -1
    for t, i in enumerate(idx):
        if i != prev and i != blank:
            out.append(charset[i - 1] if i - 1 < len(charset) else "")
            out_scores.append(float(scores[t]))
        prev = i
    score = float(np.mean(out_scores)) if out_scores else 0.0
    return "".join(out), score


def edit_distance(a, b):
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if la == 0:
        return lb
    if lb == 0:
        return la
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        ca = a[i - 1]
        for j in range(1, lb + 1):
            cost = 0 if ca == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[lb]


def cer(ref, hyp):
    if not ref:
        return 0.0 if not hyp else 1.0
    return edit_distance(ref, hyp) / len(ref)

def normalize_text(s):
    """评分用的规范化：只抹平“与识别无关的排版差异”。

    NFKC 统一全/半角，去掉所有空白，统一全角标点。不碰繁简、不改字形，
    所以它不会把识别错误洗掉——只让“空格/标点风格”这类无关差异不计入错误。
    训练标签、模型输出都保持原样，仅在评测时对两侧同用。
    """
    import re
    import unicodedata
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"\s+", "", s)
    for a, b in (("，", ","), ("。", "."), ("、", ","), ("；", ";"), ("：", ":")):
        s = s.replace(a, b)
    return s
