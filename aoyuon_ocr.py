"""Aoyuon-OCR 引擎封装 (与 XiaomiOCR / RapidOCR 同接口)

设计要点
--------
1. 上游传进来的是**字幕带**（monitor 已经裁好），这里负责把带内每一行切开，
   再逐行识别 —— 不能用整条带直接喂模型：带高 65px 压到 48px 后字高只剩一半，
   实测会让 CER 从 3% 涨到 20% 以上。
2. 模型是单行设计，折行字幕必须逐行切开分别识别再拼接，否则第二行整体丢失。
3. 没有大模型兜底了，所以用**两次裁切的投票**做自洽检查：
   两次结果一致则采信；不一致则取分数高的那个并把 score 压低，
   下游可以据此标记"该行不确定"。
"""
import numpy as np


class AoyuonOCR:
    provides_boxes = True          # 返回 (y, x, text, score)，与 ocr_frame 约定一致
    name = "aoyuon"

    def __init__(self, conf=None, log=None):
        self.conf = dict(conf or {})
        self.log = log or (lambda *a: None)
        self.rec = None
        self.dead = False
        self.threads = int(self.conf.get("threads", 4))
        self.pad_a = int(self.conf.get("pad_a", 6))      # 第一遍裁切边距
        self.pad_b = int(self.conf.get("pad_b", 12))     # 第二遍(投票用)
        self.vote = bool(self.conf.get("vote", True))
        self.min_gap = int(self.conf.get("min_gap", 2))  # 行间最小空隙(像素)
        self.contrast = int(self.conf.get("contrast", 35))

    # ------------------------------------------------------------------ 生命周期
    def start(self):
        try:
            from aoyuon.recognize import Recognizer
            self.rec = Recognizer(threads=self.threads)
            return True
        except Exception as e:                       # 缺 onnxruntime / 模型文件
            self.dead = True
            self.log(f"⚠ Aoyuon-OCR 初始化失败: {e}")
            return False

    def stop(self):
        self.rec = None

    # ------------------------------------------------------------------ 行切分
    def split_rows(self, img):
        """字幕带内按行切分 -> [(y0,y1)]

        用**相对对比**而不是绝对亮度: 字幕可能是黄/绿/红等任意颜色,
        绝对阈值(如 >=225)只对白字成立。这里比较像素与其局部背景亮度。
        """
        import cv2
        if img is None or img.size == 0:
            return []
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        h, w = g.shape[:2]
        if h < 8:
            return [(0, h)]
        # top-hat: 减去"大于字高的结构"后剩下的亮结构 = 文字本身。
        # 比"比周围亮"稳: 实心亮条不会只留上下两条边; 对彩色字幕同样成立(只看亮度差)。
        k = max(15, (h // 2) | 1)
        ker = cv2.getStructuringElement(cv2.MORPH_RECT, (max(9, w // 8), k))
        th = cv2.morphologyEx(g, cv2.MORPH_TOPHAT, ker)
        mask = (th > max(20, self.contrast - 10)).astype(np.uint8)
        rows = mask.sum(axis=1)
        if rows.max() < max(3, w * 0.01):
            return [(0, h)]                          # 找不到文字行: 整带当一行
        thr = max(3, rows.max() * 0.12)
        runs, start, gap = [], None, 0
        for y, v in enumerate(rows):
            if v > thr:
                if start is None:
                    start = y
                gap = 0
            elif start is not None:
                gap += 1
                if gap > self.min_gap:
                    runs.append((start, y - gap))
                    start, gap = None, 0
        if start is not None:
            runs.append((start, h - 1))
        runs = [(a, b) for a, b in runs if b - a >= max(6, h * 0.08)]
        if not runs:
            return [(0, h)]
        # 相邻行过近(空隙 < min_gap*2)时合并, 避免把一行切成两半
        merged = [list(runs[0])]
        for a, b in runs[1:]:
            if a - merged[-1][1] <= self.min_gap * 2:
                merged[-1][1] = b
            else:
                merged.append([a, b])
        return [(a, b) for a, b in merged]

    # ------------------------------------------------------------------ 识别

    # ------------------------------------------------------------------ 超宽行
    def _ink_cols(self, crop):
        import cv2
        g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
        bg = cv2.blur(g, (15, 15))
        m = ((g.astype(np.int16) - bg.astype(np.int16)) > 30).astype(np.uint8)
        return m.sum(axis=0)

    def _split_wide(self, crop, max_px=600, overlap=24):
        """按高度归一到 48 后的宽度超限时, 在"墨量最少"的列附近切开

        直接压扁会破坏宽高比(实测最长压扁 41%), 尾部字符被吃。
        切点优先选字间空隙(该列墨量最低), 并保留 overlap 像素重叠,
        识别后由 _join_parts 去掉接缝处重复的字。
        """
        import math
        h, w = crop.shape[:2]
        if h <= 0:
            return [crop]
        need = w * 48.0 / h
        if need <= max_px:
            return [crop]
        k = max(2, int(math.ceil(need / max_px)))
        seg = w // k
        cuts = []
        ink = self._ink_cols(crop)
        for i in range(1, k):
            center = i * seg
            lo, hi = max(1, center - seg // 4), min(w - 2, center + seg // 4)
            col = min(range(lo, hi), key=lambda c: ink[c]) if hi > lo else center
            cuts.append(col)
        parts, start = [], 0
        for c in cuts:
            parts.append(crop[:, max(0, start - (overlap if start else 0)): min(w, c + overlap)])
            start = c
        parts.append(crop[:, max(0, start - overlap):])
        return [p for p in parts if p.shape[1] >= 16]

    @staticmethod
    def _join_parts(texts):
        """拼接分块结果, 去掉接缝处重复的字"""
        out = ""
        for t in texts:
            t = (t or "").strip()
            if not t:
                continue
            if not out:
                out = t
                continue
            # 找最长重叠: out 的后缀 == t 的前缀
            ov = 0
            for k in range(min(len(out), len(t)), 0, -1):
                if out[-k:] == t[:k]:
                    ov = k
                    break
            out += t[ov:]
        return out

    def _recognize(self, crop):
        out = self.rec(crop, is_band=True) if self.rec else []
        if not out:
            return "", 0.0
        text = " ".join(o[2] for o in out if o[2]).strip()
        score = min((o[3] for o in out if o[3]), default=0.0)
        return text, float(score)

    def _recognize_row(self, crop):
        """单行识别: 超宽先分块, 再拼接"""
        parts = self._split_wide(crop)
        if len(parts) == 1:
            return self._recognize(crop)
        texts, scores = [], []
        for p in parts:
            t, sc = self._recognize(p)
            texts.append(t)
            scores.append(sc)
        return self._join_parts(texts), min(scores) if scores else 0.0

    def _recognize_vote(self, img, y0, y1):
        """两次裁切投票: 一致则采信, 不一致则降分"""
        h, w = img.shape[:2]
        a = img[max(0, y0 - self.pad_a):min(h, y1 + self.pad_a)]
        if not self.vote:
            return self._recognize_row(a)
        b = img[max(0, y0 - self.pad_b):min(h, y1 + self.pad_b)]
        t1, s1 = self._recognize_row(a)
        t2, s2 = self._recognize_row(b)
        if t1 == t2:
            return t1, max(s1, s2)
        # 不一致: 取分数高的文本, 但把置信度压到 0.6 以下便于下游标记
        best = (t1, s1) if s1 >= s2 else (t2, s2)
        return best[0], min(best[1], 0.55)

    def __call__(self, img_bgr, is_band=None):
        """img_bgr: 字幕带(或整帧)。返回 [(y, x, text, score)]"""
        if self.dead or self.rec is None or img_bgr is None:
            return []
        try:
            import cv2
            if img_bgr.ndim == 2:
                img_bgr = cv2.cvtColor(img_bgr, cv2.COLOR_GRAY2BGR)
            h = img_bgr.shape[0]
            rows = self.split_rows(img_bgr)
            out = []
            for y0, y1 in rows:
                text, score = self._recognize_vote(img_bgr, y0, y1)
                if text:
                    out.append((float(y0), 0.0, text, score))
            return out
        except Exception as e:
            self.log(f"⚠ Aoyuon-OCR 识别异常: {e}")
            return []
