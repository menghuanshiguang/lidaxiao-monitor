"""AoyuonOCR 引擎单测: 行切分 + 双次裁切投票 (不需要 onnxruntime)

用假识别器替代真模型: 按裁切图的高度返回不同文本, 用于验证投票与行切分逻辑。
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import aoyuon_ocr as A  # noqa: E402

ok = fail = 0


def chk(name, cond, info=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✅ {name}")
    else:
        fail += 1
        print(f"  ❌ {name}\n     {info}")


class FakeRec:
    """根据输入图高度返回不同文本, 模拟"裁切不同 -> 结果不同" """

    def __init__(self, texts):
        self.texts = texts
        self.calls = 0

    def __call__(self, img, is_band=True):
        self.calls += 1
        h = img.shape[0]
        t = self.texts(h)
        return [(0.0, 0.0, t, 0.9)] if t else []


def make_band(lines=1, h=80, w=320):
    """造一条字幕带: 黑底 + 白字条(相对对比足够)"""
    img = np.zeros((h, w, 3), np.uint8)
    if lines == 1:
        img[h // 2 - 12:h // 2 + 12, 20:w - 20] = 255
    else:
        img[8:32, 20:w - 20] = 255
        img[48:72, 20:w - 20] = 255
    return img


print("行切分")
eng = A.AoyuonOCR()
rows1 = eng.split_rows(make_band(lines=1))
chk("单行 -> 1 段", len(rows1) == 1, rows1)
rows2 = eng.split_rows(make_band(lines=2))
chk("两行 -> 2 段", len(rows2) == 2, rows2)
chk("两行位置合理", len(rows2) == 2 and rows2[0][0] < rows2[1][0], rows2)
chk("空图 -> 空", eng.split_rows(np.zeros((0, 0, 3), np.uint8)) == [])

print("委托给原生 Recognizer")
class Delegating:
    provides_boxes = True
    def __init__(self):
        self.seen = 0
    def __call__(self, img, is_band=None):
        self.seen += 1
        return [(10.0, 20.0, "识别结果", 0.93), (40.0, 20.0, "第二行", 0.91)]

eng2 = A.AoyuonOCR()
eng2.rec = Delegating()
res = eng2(make_band(lines=2))
chk("原样透传结果", res == [(10.0, 20.0, "识别结果", 0.93), (40.0, 20.0, "第二行", 0.91)], res)
chk("确实调用了原生", eng2.rec.seen == 1, eng2.rec.seen)

class Boom:
    provides_boxes = True
    def __call__(self, img, is_band=None):
        raise RuntimeError("模型炸了")

eng3 = A.AoyuonOCR()
eng3.rec = Boom()
chk("异常时返回空而不抛", eng3(make_band()) == [])

print("未初始化保护")
eng5 = A.AoyuonOCR()
chk("未 start -> 空结果", eng5(make_band()) == [])

print(f"\n{ok} passed, {fail} failed")
raise SystemExit(1 if fail else 0)
