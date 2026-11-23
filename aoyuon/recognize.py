"""单行字幕识别器 —— 与现有 OCR 引擎同构的接口, 纯 CPU / 不联网。

    from recognize import Recognizer
    rec = Recognizer("distill/model")
    boxes = rec(img_bgr)          # -> [(y, x, text, score), ...]
    rec.provides_boxes == True

`img_bgr` 可以是整帧(内部自己找画面下方的字幕带)也可以是已经裁好的字幕带。

命令行:
    python distill/recognize.py --image x.png
"""
import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from band import find_line_boxes  # noqa: E402
from common import (ctc_greedy, preprocess, read_charset, read_config,  # noqa: E402
                    restore_punct, split_wide)
from onnx_engine import OnnxCRNN  # noqa: E402

DEFAULT_MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model")


class Recognizer:
    provides_boxes = True

    def __init__(self, model_dir=DEFAULT_MODEL_DIR, threads=4, min_score=0.0, punct=False,
                 pad_x=10, pad_y=6, lm_path=None, lm_weight=0.3, beam=8,
                 neighbor=True, chunk_wide=True, score_mode="geo",
                 band_mode="auto", norm_height=None, wide_thr=1.35):
        # pad_x/pad_y 必须与 build_dataset.py 的裁图边距一致: 训练裁图和推理裁图
        # 的上下留白不同会让字重/字高分布漂移, 直接表现为"训练集 70% 命中, 整帧全错"。
        # neighbor: 是否启用"邻行兜底"(找折行的第二行短行)。默认开, 关掉可复现旧行为。
        self.neighbor = bool(neighbor)
        # chunk_wide: 超宽行是否分块识别(False 时退回旧行为: 直接压扁, 会静默掉字)
        self.chunk_wide = bool(chunk_wide)
        # wide_thr: 只有自然宽度 > wide_thr × max_width 才分块 (issue #15)。
        # 实测(8346 行 test): 轻度超宽(<=1.35×)压扁反而更准 —— 训练裁图走的就是
        # 同一条压扁路径, 压扁在分布内; 切块引入切点边界属于分布外。只有极端超宽
        # (>=2×, 横拼长条) 分块才明显更好(整行 3.3% -> 51.7%)。
        self.wide_thr = float(wide_thr)
        # punct: 解码后把半角标点还原成中文全角(见 common.restore_punct)。
        # issue #18: 0.2 起训练标签保持老师原样(含全角标点), 模型自己就会输出全角,
        # 所以这里的启发式默认关闭; 只有加载 0.1 时代(标点被归一化)的权重时才需要打开。
        self.punct = bool(punct)
        # band_mode: 传入已裁好的字幕带时怎么处理 (issue #16)
        #   "auto"   先尝试按行切分, 切出 >=2 行就逐行识别, 否则整带当一行
        #   "single" 整带当一行 (0.2 之前的行为, 双行带会丢第二行)
        #   "multi"  强制按行切分, 切不出来才退化整带一行
        if band_mode not in ("auto", "single", "multi"):
            raise ValueError(f"band_mode 只支持 auto/single/multi, 收到 {band_mode!r}")
        self.band_mode = band_mode
        # norm_height: 整帧先等比缩放到这个高度再做检测 (issue #17)。
        # 训练分布是 480p(帧高 480), 1080p 直接送入会掉 ~9 个点; 传 480 可拉回。
        self.norm_height = int(norm_height) if norm_height else None
        self.pad_x = int(pad_x)
        self.pad_y = int(pad_y)
        self.model_dir = model_dir
        self.charset = read_charset(os.path.join(model_dir, "charset.txt"))
        cfgp = os.path.join(model_dir, "config.json")
        self.config = read_config(cfgp) if os.path.exists(cfgp) else {}
        self.height = int(self.config.get("height", 48))
        self.max_width = int(self.config.get("max_width", 640))
        self.blank = int(self.config.get("blank", 0))
        self.min_score = float(min_score)
        # score_mode: 整行置信度算法 (issue #8: geo 的可分性最好, AUC 0.78 vs mean 0.77;
        # 0~1, 不含 LM 项; 只在贪心解码下生效)
        self.score_mode = score_mode
        # 优先 int8 部署模型, 退到 fp32
        cand = [os.path.join(model_dir, "model.onnx"),
                os.path.join(model_dir, "model_int8.onnx"),
                os.path.join(model_dir, "model_fp32.onnx")]
        path = next((p for p in cand if os.path.exists(p)), None)
        if path is None:
            raise FileNotFoundError(f"{model_dir} 下没有 model.onnx / model_fp32.onnx")
        self.model_path = path
        self.engine = OnnxCRNN(path, threads=threads)
        # 可选的语言模型重排 (默认关闭: 贪心解码 p50 十几毫秒, 束搜索 +20~40ms)
        self.lm = None
        self.lm_weight = float(lm_weight)
        self.beam = int(beam)
        if lm_path:
            from ctc_lm import CharNGram
            self.lm = CharNGram.load(lm_path)

    # ------------------------------------------------------------------ 内部
    @staticmethod
    def _is_band(img):
        """整帧 vs 已裁好的字幕带: 字幕带又扁又宽。"""
        h, w = img.shape[:2]
        return h <= 130 or h <= int(0.30 * w)

    def _pad_boxes(self, boxes, h, w):
        out = []
        for y0, y1, x0, x1 in boxes:
            out.append((max(0, y0 - self.pad_y), min(h, y1 + self.pad_y),
                        max(0, x0 - self.pad_x), min(w, x1 + self.pad_x)))
        return out

    def _run_one(self, piece, want_chars=False):
        x = preprocess(piece, height=self.height, max_width=self.max_width)
        logits = self.engine.logits(x)
        if self.lm is None:
            return ctc_greedy(logits, self.charset, blank=self.blank,
                              score_mode=self.score_mode, return_chars=want_chars)
        from ctc_lm import ctc_beam_lm
        if want_chars:
            # 束搜索不产生逐帧对齐, 逐字置信度只在贪心解码下有意义
            raise RuntimeError("逐字置信度只支持贪心解码, 请去掉 lm_path")
        return ctc_beam_lm(logits, self.charset, self.lm, beam=self.beam,
                           lm_weight=self.lm_weight, blank=self.blank)

    def _run(self, crop_bgr, want_chars=False):
        """单行裁剪 -> (text, score, info) 或 (text, score, info, charinfo)。

        超过 max_width 的行不再静默压扁: 按字间空隙切块分别识别再拼回去,
        info["overflow"]=True 且 info["n"]>1, 调用方可在 meta 里看到。
        """
        hImg, wImg = crop_bgr.shape[:2]
        nw = max(8, int(round(wImg * self.height / float(hImg))))
        if self.chunk_wide and nw > self.max_width * self.wide_thr:
            chunks, info = split_wide(crop_bgr, height=self.height,
                                      max_width=self.max_width)
        else:
            chunks, info = [crop_bgr], {"n": 1, "split_x": [], "overflow": nw > self.max_width}
        if info["n"] == 1:
            r = self._run_one(chunks[0], want_chars=want_chars)
            text, score = r[0], r[1]
            ci = r[2] if want_chars else None
            if want_chars:
                return self._post(text), float(score), info, ci
            return self._post(text), float(score), info
        texts, scores, cis = [], [], []
        for c in chunks:
            r = self._run_one(c, want_chars=want_chars)
            t, sc = r[0], r[1]
            if t:
                texts.append(t)
                scores.append(float(sc))
                if want_chars:
                    cis.append(r[2])
        text = self._post("".join(texts))
        score = (min(scores) if scores else 0.0)
        if not want_chars:
            return text, score, info
        merged = {"chars": [], "char_scores": [], "char_pos": []}
        for ci in cis:
            base = len(merged["chars"])
            merged["chars"] += ci["chars"]
            merged["char_scores"] += ci["char_scores"]
            merged["char_pos"] += [base + p for p in ci["char_pos"]]
        return text, score, info, merged

    def _post(self, text):
        """解码后处理: 标点风格还原（见 common.restore_punct 的说明）。

        v7 训练标签把全角标点归一成了半角，模型学到的是半角标点；这里还原成
        中文全角，保证输出风格与老师一致。数字内部（1,000 / 1.5）不动。
        """
        return restore_punct(text) if self.punct else text

    def _band_lines(self, img):
        """已裁好的字幕带 -> 按行切分 (issue #16)。

        双行字幕带整块送进单行模型会把两行压进同一个 CTC 序列, 第二行整段丢失,
        且不报错。这里用检测器把带子切成行框, 切不出 (<2 行) 时调用方退化整带一行。
        """
        h, w = img.shape[:2]
        bs = find_line_boxes(img, box=(0, h, 0, w), neighbor=self.neighbor)
        return self._pad_boxes(bs, h, w)

    def recognize_with_chars(self, crop_bgr):
        """单行裁剪 -> (text, score, charinfo) （issue #19）。

        charinfo = {"chars": [...], "char_scores": [...], "char_pos": [...]}
        逐字置信度只支持贪心解码 (开启 LM 时会抛错)。
        """
        if crop_bgr is None or not isinstance(crop_bgr, np.ndarray) or crop_bgr.size == 0:
            return "", 0.0, {"chars": [], "char_scores": [], "char_pos": []}
        if crop_bgr.ndim == 2:
            crop_bgr = np.stack([crop_bgr] * 3, axis=-1)
        text, score, _info, ci = self._run(crop_bgr, want_chars=True)
        return text, score, ci

    # -------------------------------------------------------------- 对外接口
    def __call__(self, img_bgr, is_band=None):
        """img_bgr: cv2 BGR ndarray。返回 [(y, x, text, score), ...]。

        行框来自 band.find_line_boxes —— 与标注/训练裁图是同一个检测器, 训练和
        推理看到的框分布一致。整帧找不到字幕行时按契约退到底部 30%。
        """
        if img_bgr is None or not isinstance(img_bgr, np.ndarray) or img_bgr.size == 0:
            return []
        if img_bgr.ndim == 2:
            img_bgr = np.stack([img_bgr] * 3, axis=-1)
        img = img_bgr
        h, w = img.shape[:2]
        self.last_meta = []
        if is_band is None:
            is_band = self._is_band(img)

        # issue #17: 整帧先归一到训练尺度 (480p 训练 -> 1080p 输入掉 ~9 pt)
        inv = 1.0
        if (not is_band) and self.norm_height and \
                abs(h - self.norm_height) > 0.2 * self.norm_height:
            import cv2
            scale = self.norm_height / float(h)
            img = cv2.resize(img, (max(8, int(round(w * scale))), self.norm_height),
                             interpolation=cv2.INTER_AREA)
            inv = float(h) / img.shape[0]
            h, w = img.shape[:2]

        boxes = self._pad_boxes(find_line_boxes(img, neighbor=self.neighbor), h, w)
        out = []
        if boxes:
            for y0, y1, x0, x1 in boxes:
                text, score, info = self._run(img[y0:y1, x0:x1])
                info = dict(info, y=float(y0 * inv), x=float(x0 * inv))
                self.last_meta.append(info)
                if text and score >= self.min_score:
                    out.append((float(y0 * inv), float(x0 * inv), text, score))
            return out

        if is_band:
            # 已裁好的字幕带: 双行带必须逐行识别 (issue #16)
            bl = [] if self.band_mode == "single" else self._band_lines(img)
            if len(bl) >= 2:
                for y0, y1, x0, x1 in bl:
                    text, score, info = self._run(img[y0:y1, x0:x1])
                    self.last_meta.append(dict(info, y=float(y0), x=float(x0)))
                    if text and score >= self.min_score:
                        out.append((float(y0), float(x0), text, score))
                return out
            text, score, info = self._run(img)
            self.last_meta.append(dict(info, y=0.0, x=0.0))
            if text and score >= self.min_score:
                return [(0.0, 0.0, text, score)]
            return []

        # 整帧但没检测到字幕行: 契约兜底, 取底部 30%
        y_off = int(h * 0.70)
        band = img[y_off:]
        bb = self._pad_boxes(find_line_boxes(band, neighbor=self.neighbor),
                            band.shape[0], band.shape[1])
        for by0, by1, bx0, bx1 in bb:
            text, score, info = self._run(band[by0:by1, bx0:bx1])
            self.last_meta.append(dict(info, y=float((y_off + by0) * inv),
                                       x=float(bx0 * inv)))
            if text and score >= self.min_score:
                out.append((float((y_off + by0) * inv), float(bx0 * inv), text, score))
        return out


def main():
    ap = argparse.ArgumentParser(description="单行中文字幕识别")
    ap.add_argument("--image", required=True, help="图片路径 (整帧或字幕带)")
    ap.add_argument("--model", default=DEFAULT_MODEL_DIR)
    ap.add_argument("--score-mode", default="geo", choices=["mean", "min", "geo"])
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--no-punct", action="store_true",
                    help="(兼容保留) 不把半角标点还原成全角; 0.2 起默认就不还原")
    ap.add_argument("--punct", action="store_true",
                    help="启用标点还原启发式 (只有加载 0.1 时代权重时才需要)")
    ap.add_argument("--band-mode", default="auto", choices=["auto", "single", "multi"],
                    help="字幕带处理方式: auto 先按行切, single 整带一行, multi 强制切行")
    ap.add_argument("--norm-height", type=int, default=None,
                    help="整帧先等比缩放到这个高度再检测 (480p 训练 -> 传 480)")
    ap.add_argument("--chars", action="store_true",
                    help="--json 时附带逐字置信度")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--lm", default=None, help="字符 n-gram LM 路径 (开启束搜索解码)")
    ap.add_argument("--lm-weight", type=float, default=0.3)
    args = ap.parse_args()

    import cv2
    img = cv2.imread(args.image)
    if img is None:
        raise SystemExit(f"读不到图片: {args.image}")
    rec = Recognizer(args.model, threads=args.threads, punct=args.punct,
                     band_mode=args.band_mode, norm_height=args.norm_height,
                     lm_path=args.lm, lm_weight=args.lm_weight)
    boxes = rec(img)
    if args.json and args.chars:
        # 逐字置信度按"单行图"给: 传整帧时对检测出的每一行分别再解一次
        payload = []
        if rec._is_band(img):
            rows = [(0.0, 0.0, img)]
        else:
            bb = rec._pad_boxes(find_line_boxes(img, neighbor=rec.neighbor),
                                img.shape[0], img.shape[1])
            rows = [(float(y0), float(x0), img[y0:y1, x0:x1]) for y0, y1, x0, x1 in bb]
        for y, x, crop in rows:
            text, score, ci = rec.recognize_with_chars(crop)
            payload.append({"y": y, "x": x, "text": text,
                            "score": round(float(score), 4),
                            "chars": ci["chars"],
                            "char_scores": [round(v, 4) for v in ci["char_scores"]]})
        print(json.dumps(payload, ensure_ascii=False))
        return
    if args.json:
        print(json.dumps([[y, x, t, round(s, 4)] for y, x, t, s in boxes],
                         ensure_ascii=False))
    else:
        if not boxes:
            print("")
        for y, x, t, s in boxes:
            print(f"{y}\t{x}\t{t}\t{s:.3f}")


if __name__ == "__main__":
    main()
