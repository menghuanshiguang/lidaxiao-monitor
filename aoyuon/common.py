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


def ctc_greedy(logits, charset, blank=BLANK, score_mode="mean", return_chars=False):
    """logits: (T, C) 或 (C, T) 的 log-probs 或 probs; 返回 (text, score)。

    score_mode 决定整行置信度的算法 (issue #8 实测: geo 的可分性最好):
      mean : 逐字最大概率的均值 (历史默认)
      min  : 逐字最大概率的最小值 (最弱那个字)
      geo  : 逐字最大概率的几何平均 = exp(mean(log p))

    return_chars=True 时多返回一个 dict（issue #19）:
      {"chars": [...], "char_scores": [...], "char_pos": [...]}
    char_scores 是每个字在它那几帧上的最大概率均值, 下游可据此只复核数字/关键字符。
    """
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
    out, out_scores, out_ts = [], [], []
    prev = -1
    for t, i in enumerate(idx):
        if i != prev and i != blank:
            out.append(charset[i - 1] if i - 1 < len(charset) else "")
            out_scores.append(float(scores[t]))
            out_ts.append(t)
        prev = i
    if not out_scores:
        if return_chars:
            return "", 0.0, {"chars": [], "char_scores": [], "char_pos": []}
        return "", 0.0
    arr = np.asarray(out_scores, dtype=np.float64)
    if score_mode == "min":
        score = float(arr.min())
    elif score_mode == "geo":
        score = float(np.exp(np.log(np.maximum(arr, 1e-12)).mean()))
    else:
        score = float(arr.mean())
    text = "".join(out)
    if return_chars:
        # issue #19: 逐字置信度。CTC 里同一个字可能占据多帧（重复帧被合并），
        # 这里把属于该字的所有帧的最大概率取均值，比只看第一帧更稳。
        char_scores = []
        for j, t in enumerate(out_ts):
            t_end = out_ts[j + 1] if j + 1 < len(out_ts) else len(idx)
            cls = idx[t]
            span = np.arange(t, t_end)
            span = span[idx[span] == cls]
            char_scores.append(float(scores[span].mean()) if span.size else out_scores[j])
        return text, score, {"chars": list(out), "char_scores": char_scores,
                             "char_pos": list(range(len(out)))}
    return text, score


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

_FW_PUNCT = {",": "，", ".": "。", ":": "：", ";": "；", "?": "？", "!": "！",
             "(": "（", ")": "）"}


def restore_punct(s):
    """把输出里的半角标点还原成中文全角标点（数字内部不动）。

    为什么需要：数据集 v7 的训练标签被 `canonical_text` 顺手把全角标点也归一成了
    半角（`，`->`,`），模型因此学会了半角标点，与老师原样输出的风格不一致 ——
    逐字比对时每行带标点的样本都会被判错。这一步在**解码后**还原，数字里的
    `1,000` / `1.5`（两侧都是数字）保持不变。

    注意：顿号 `、` 在标签规范化时已经被并成 `,`，无法还原，见 eval.md 的说明。
    """
    if not s:
        return s
    out = []
    n = len(s)
    for i, ch in enumerate(s):
        if ch in ".,:":
            prev = s[i - 1] if i > 0 else ""
            nxt = s[i + 1] if i + 1 < n else ""
            if prev.isdigit() and nxt.isdigit():
                out.append(ch)
                continue
        out.append(_FW_PUNCT.get(ch, ch))
    return "".join(out)


def normalize_text(s):
    """评分用的规范化：只抹平“与识别无关的写法差异”(issue #14)。

    NFKC 统一全/半角，去掉所有空白，统一全角标点，并把数字串里的千分位
    统一成半角逗号（`1.000亿` / `1、000亿` -> `1,000亿`，仅当正好 3 位且紧跟量级词）。
    不碰繁简、不改字形，所以它不会把识别错误洗掉。
    训练标签、模型输出都保持原样，仅在评测时对两侧同用。
    """
    import re
    import unicodedata
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"\s+", "", s)
    for a, b in (("，", ","), ("。", "."), ("、", ","), ("；", ";"), ("：", ":")):
        s = s.replace(a, b)
    s = re.sub(r"(?<=\d)[.、,·](\d{3})(?=(亿|万|千|百|元|美元|港元|万亿|个百分点|%))",
               r",\1", s)
    s = re.sub(r"(?<=\d)\s*%", "%", s)
    return s


def load_t2s(path=None):
    """繁简映射表（由 opencc t2s 在本机生成，见 distill/data/t2s.json）。

    没有表时返回空 dict —— 调用方降级为"不做繁简归一"，不抛异常。
    """
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "data", "t2s.json")
    path = os.path.abspath(path)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def to_simplified(s, table=None):
    """繁 -> 简（issue #12）。table 缺省用 load_t2s()。"""
    if not s:
        return s
    m = table if table is not None else load_t2s()
    if not m:
        return s
    return "".join(m.get(ch, ch) for ch in s)


def canonical_text(s, table=None):
    """评测/训练共用的最终规范形：繁简归一 + 数字标点归一。"""
    return normalize_text(to_simplified(s, table))


def is_digit_row(s):
    """数字子集：含阿拉伯数字或百分号的行（issue #14 要求单独报这一格）。"""
    import re
    return bool(re.search(r"[0-9%]", s or ""))


def split_wide(crop_bgr, height=IMG_H, max_width=MAX_W, chunk_max=None):
    """把超宽的单行图切成若干块，每块缩放后宽度 <= max_width（issue #11）。

    超宽行（片尾合规声明、荐书卡片）直接 resize 到 640 会被横向压扁，字宽/字高
    比例偏离训练分布，结果是静默掉字 —— 所以按字间空隙切成 2~4 块分别识别再拼接。

    返回 (chunks, info)：chunks 是 BGR 子图列表，info = {"n", "split_x", "overflow"}。
    """
    import cv2
    h, w = crop_bgr.shape[:2]
    if h <= 0 or w <= 0:
        raise ValueError("empty crop")
    nw = max(8, int(round(w * height / float(h))))
    if nw <= max_width:
        return [crop_bgr], {"n": 1, "split_x": [], "overflow": False}
    chunk_max = int(chunk_max or max(8, int(max_width * 0.75)))
    k = max(2, int(np.ceil(nw / float(chunk_max))))
    g = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY) if crop_bgr.ndim == 3 else crop_bgr
    # 每列"墨迹量"：暗像素(描边) + 亮像素(字身)，用来找字间空隙
    ink = (g < 96).sum(axis=0).astype(np.int32) + (g > 200).sum(axis=0).astype(np.int32)
    width_px = int(round(chunk_max * h / float(height)))
    cuts = [0]
    for i in range(1, k):
        target = int(round(i * w / float(k)))
        lo = max(cuts[-1] + 8, target - width_px // 3)
        hi = min(w - 1, target + width_px // 3)
        if hi <= lo:
            cut = min(w - 1, max(cuts[-1] + 8, target))
        else:
            cut = lo + int(np.argmin(ink[lo:hi]))
        if cut - cuts[-1] < 8:
            cut = min(w - 1, cuts[-1] + 8)
        cuts.append(cut)
    cuts.append(w)
    chunks = [crop_bgr[:, cuts[i]:cuts[i + 1]] for i in range(len(cuts) - 1)]
    return chunks, {"n": len(chunks), "split_x": cuts[1:-1], "overflow": True}
