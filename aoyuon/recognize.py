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
from common import ctc_greedy, preprocess, read_charset, read_config  # noqa: E402
from onnx_engine import OnnxCRNN  # noqa: E402

DEFAULT_MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model")


class Recognizer:
    provides_boxes = True

    def __init__(self, model_dir=DEFAULT_MODEL_DIR, threads=4, min_score=0.0,
                 pad_x=10, pad_y=6, lm_path=None, lm_weight=0.3, beam=8):
        # pad_x/pad_y 必须与 build_dataset.py 的裁图边距一致: 训练裁图和推理裁图
        # 的上下留白不同会让字重/字高分布漂移, 直接表现为"训练集 70% 命中, 整帧全错"。
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

    def _run(self, crop_bgr):
        x = preprocess(crop_bgr, height=self.height, max_width=self.max_width)
        logits = self.engine.logits(x)
        if self.lm is None:
            text, score = ctc_greedy(logits, self.charset, blank=self.blank)
        else:
            from ctc_lm import ctc_beam_lm
            text, score = ctc_beam_lm(logits, self.charset, self.lm,
                                      beam=self.beam, lm_weight=self.lm_weight,
                                      blank=self.blank)
        return text, float(score)

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

        boxes = self._pad_boxes(find_line_boxes(img), h, w)
        out = []
        if boxes:
            for y0, y1, x0, x1 in boxes:
                text, score = self._run(img[y0:y1, x0:x1])
                if text and score >= self.min_score:
                    out.append((float(y0), float(x0), text, score))
            return out

        if is_band is None:
            is_band = self._is_band(img)
        if is_band:
            # 已裁好的字幕带: 整块当一行
            text, score = self._run(img)
            if text and score >= self.min_score:
                return [(0.0, 0.0, text, score)]
            return []

        # 整帧但没检测到字幕行: 契约兜底, 取底部 30%
        y_off = int(h * 0.70)
        band = img[y_off:]
        bb = self._pad_boxes(find_line_boxes(band), band.shape[0], band.shape[1])
        for by0, by1, bx0, bx1 in bb:
            text, score = self._run(band[by0:by1, bx0:bx1])
            if text and score >= self.min_score:
                out.append((float(y_off + by0), float(bx0), text, score))
        return out


def main():
    ap = argparse.ArgumentParser(description="单行中文字幕识别")
    ap.add_argument("--image", required=True, help="图片路径 (整帧或字幕带)")
    ap.add_argument("--model", default=DEFAULT_MODEL_DIR)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--lm", default=None, help="字符 n-gram LM 路径 (开启束搜索解码)")
    ap.add_argument("--lm-weight", type=float, default=0.3)
    args = ap.parse_args()

    import cv2
    img = cv2.imread(args.image)
    if img is None:
        raise SystemExit(f"读不到图片: {args.image}")
    rec = Recognizer(args.model, threads=args.threads, lm_path=args.lm, lm_weight=args.lm_weight)
    boxes = rec(img)
    if args.json:
        print(json.dumps([[y, x, t, round(s, 4)] for y, x, t, s in boxes], ensure_ascii=False))
    else:
        if not boxes:
            print("")
        for y, x, t, s in boxes:
            print(f"{y}\t{x}\t{t}\t{s:.3f}")


if __name__ == "__main__":
    main()
